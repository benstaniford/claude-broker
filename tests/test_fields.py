import json
import unittest

from tests import helpers  # noqa: F401  (puts the repo on sys.path)
from claude_broker.fields import FieldStream, read_string, scan


def deltas(stream: FieldStream, text: str, step: int, index: int = 1) -> list[dict]:
    out = []
    for i in range(0, len(text), step):
        out += stream.feed({"type": "content_block_delta", "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": text[i:i + step]}})
    return out


def start(index: int = 1, name: str = "StructuredOutput") -> dict:
    return {"type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "name": name, "input": {}}}


def rebuild(events: list[dict], name: str) -> str:
    text = ""
    for e in events:
        if e["type"] == "fields_reset":
            text = ""
        elif e.get("name") == name and e["type"] == "field":
            text = text + e["delta"] if "delta" in e else e["text"]
    return text


class ReadStringTest(unittest.TestCase):
    def test_escapes_and_cut_offs(self):
        self.assertEqual(read_string('a\\nb" rest', 0), ("a\nb", 5))
        self.assertEqual(read_string("ab\\", 0), ("ab", None))            # an escape cut off
        self.assertEqual(read_string("ab\\u00", 0), ("ab", None))
        self.assertEqual(read_string("\\u00e9\"", 0), ("é", 7))
        self.assertEqual(read_string("\\ud83d", 0), ("", None))           # waiting for the low half
        self.assertEqual(read_string("\\ud83d\\ude00\"", 0), ("😀", 13))
        self.assertEqual(read_string("\\ud83dxxxxxx\"", 0)[0], "xxxxxx")  # a lone surrogate dropped


class ScanTest(unittest.TestCase):
    def test_every_string_field_any_order(self):
        doc = {"n": 3, "deep": {"a": "}", "b": [1, "]"]}, "first": "x", "flag": True, "second": "y\"z", "nul": None}
        self.assertEqual(scan(json.dumps(doc)), {"first": ("x", True), "second": ('y"z', True)})

    def test_every_prefix_is_safe(self):
        text = json.dumps({"a": "one \\ two \u00e9 \U0001F600", "n": [1, {"x": "y"}], "b": "three"})
        seen_a = ""
        for i in range(len(text) + 1):
            fields = scan(text[:i])
            if "a" in fields:
                self.assertTrue(fields["a"][0].startswith(seen_a))          # only ever grows
                seen_a = fields["a"][0]
        self.assertEqual(scan(text)["b"], ("three", True))

    def test_not_an_object(self):
        self.assertEqual(scan(""), {})
        self.assertEqual(scan('["a"]'), {})


class FieldStreamTest(unittest.TestCase):
    def test_deltas_add_up_and_close_once(self):
        stream = FieldStream(["narration", "scene"])
        reply = {"narration": "Arrows hiss \u2014 from the brush.", "x": {"scene": "no"}, "scene": "goblins"}
        self.assertEqual(stream.feed(start()), [])
        events = deltas(stream, json.dumps(reply), 3)
        self.assertEqual(rebuild(events, "narration"), reply["narration"])
        closed = [e for e in events if e["type"] == "field_closed"]
        self.assertEqual([(e["name"], e["text"]) for e in closed],
                         [("narration", reply["narration"]), ("scene", "goblins")])
        self.assertFalse(any(e.get("name") == "x" for e in events))

    def test_other_blocks_ignored_and_retry_resets(self):
        stream = FieldStream(["narration"])
        stream.feed(start(0, "Read"))
        self.assertEqual(deltas(stream, '{"narration": "no"}', 4, index=0), [])
        stream.feed(start(2))
        first = deltas(stream, '{"narration": "first try', 5, index=2)
        self.assertEqual(rebuild(first, "narration"), "first try")
        self.assertEqual(stream.feed(start(4)), [{"type": "fields_reset"}])
        again = deltas(stream, '{"narration": "again"}', 5, index=4)
        self.assertEqual(rebuild(first + [{"type": "fields_reset"}] + again, "narration"), "again")

    def test_nothing_wanted(self):
        stream = FieldStream([])
        stream.feed(start())
        self.assertEqual(deltas(stream, '{"narration": "x"}', 2), [])


if __name__ == "__main__":
    unittest.main()
