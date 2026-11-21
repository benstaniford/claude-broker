"""Top-level string fields of a structured reply, read while claude is still writing it.

With `--include-partial-messages`, claude streams its StructuredOutput tool call as
`input_json_delta` fragments: a JSON object that can stop anywhere (inside a key, a value,
an escape or a surrogate pair). `scan` reads what is there so far; `FieldStream` follows the
stream events and reports each wanted field as it grows and when it closes.
"""

from __future__ import annotations

import re

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_LOW_SURROGATE = re.compile(r"[dD][c-fC-F][0-9a-fA-F]{2}")
_WS = " \t\r\n"


def read_string(buf: str, i: int) -> tuple[str, int | None]:
    """A JSON string's text from just after its opening quote, and the index after its closing
    quote (None while it is still being written)."""
    out, n = [], len(buf)
    while i < n:
        ch = buf[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break                                   # an escape cut off
        esc = buf[i + 1]
        if esc != "u":
            out.append(_ESCAPES.get(esc, esc))
            i += 2
            continue
        hexa = buf[i + 2:i + 6]
        if len(hexa) < 4 or not _HEX4.fullmatch(hexa):
            break
        code = int(hexa, 16)
        if 0xD800 <= code < 0xDC00:                 # a high surrogate: wait for its pair
            low = buf[i + 6:i + 12]
            if len(low) < 6:
                break
            if low[:2] == "\\u" and _LOW_SURROGATE.fullmatch(low[2:]):
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (int(low[2:], 16) - 0xDC00)))
                i += 12
                continue
            i += 6                                  # a lone surrogate: drop it
            continue
        if 0xDC00 <= code < 0xE000:
            i += 6
            continue
        out.append(chr(code))
        i += 6
    return "".join(out), None


def _skip_ws(buf: str, i: int) -> int:
    while i < len(buf) and buf[i] in _WS:
        i += 1
    return i


def _skip_value(buf: str, i: int) -> int | None:
    """The index after a JSON value that isn't a string at the top level (an object, array,
    number or literal), or None when it isn't complete yet."""
    n = len(buf)
    if i >= n:
        return None
    if buf[i] == '"':
        return read_string(buf, i + 1)[1]
    if buf[i] in "{[":
        depth = 0
        while i < n:
            ch = buf[i]
            if ch == '"':
                end = read_string(buf, i + 1)[1]
                if end is None:
                    return None
                i = end
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return None
    while i < n and buf[i] not in ",}]" + _WS:   # a number, true, false or null
        i += 1
    return i if i < n else None


def scan(buf: str) -> dict[str, tuple[str, bool]]:
    """Every top-level string field written so far: name → (text, closed). A field whose
    value has begun but not ended is there with closed False; fields of other types are
    skipped. Stops at the first thing that isn't complete."""
    fields: dict[str, tuple[str, bool]] = {}
    i = _skip_ws(buf, 0)
    if i >= len(buf) or buf[i] != "{":
        return fields
    i += 1
    while True:
        i = _skip_ws(buf, i)
        if i >= len(buf) or buf[i] != '"':
            return fields
        key, end = read_string(buf, i + 1)
        if end is None:
            return fields
        i = _skip_ws(buf, end)
        if i >= len(buf) or buf[i] != ":":
            return fields
        i = _skip_ws(buf, i + 1)
        if i >= len(buf):
            return fields
        if buf[i] == '"':
            text, end = read_string(buf, i + 1)
            fields[key] = (text, end is not None)
            if end is None:
                return fields
            i = end
        else:
            end = _skip_value(buf, i)
            if end is None:
                return fields
            i = end
        i = _skip_ws(buf, i)
        if i >= len(buf) or buf[i] != ",":
            return fields
        i += 1


class FieldStream:
    """Follows claude's stream events and reports the wanted fields of the StructuredOutput
    call being written. A new StructuredOutput call (claude trying again after a schema error)
    starts again from nothing, reported as one `fields_reset` event."""

    def __init__(self, wanted: list[str] | tuple[str, ...]):
        self.wanted = tuple(wanted)
        self.index: int | None = None
        self.buf = ""
        self.sent: dict[str, tuple[str, bool]] = {}

    def feed(self, event) -> list[dict]:
        """The events this stream event gives, in order: `field` (`delta`: text to append to
        what came before; or `text`, to replace it, should it ever not simply grow),
        `field_closed` (the whole text, once) and `fields_reset`."""
        if not self.wanted or not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                restarted = bool(self.sent)
                self.index, self.buf, self.sent = event.get("index"), "", {}
                return [{"type": "fields_reset"}] if restarted else []
            return []
        if kind != "content_block_delta" or self.index is None or event.get("index") != self.index:
            return []
        delta = event.get("delta") or {}
        if delta.get("type") != "input_json_delta":
            return []
        self.buf += str(delta.get("partial_json") or "")
        out = []
        for name, (text, closed) in scan(self.buf).items():
            if name not in self.wanted:
                continue
            before = self.sent.get(name)
            if before == (text, closed):
                continue
            prev = before[0] if before else ""
            if text != prev:
                if text.startswith(prev):
                    out.append({"type": "field", "name": name, "delta": text[len(prev):]})
                else:
                    out.append({"type": "field", "name": name, "text": text})
            if closed:
                out.append({"type": "field_closed", "name": name, "text": text})
            self.sent[name] = (text, closed)
        return out
