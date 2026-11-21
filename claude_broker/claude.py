"""Running claude: the locked-down command, the process, and its stream-json output.

Adapted from the Dungeons & Claude bridge (scripts/dm-bridge.py there). Every run:
- reads only project settings (the profile's), never the user's permissions, hooks or MCP;
- loads no ancestor CLAUDE.md (e.g. ~/CLAUDE.md);
- has only the built-in tools asked for, and permission rules for the folders asked for;
- refuses anything else rather than prompting (`dontAsk`).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BUILTIN_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Skill")
READ_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Edit", "Write")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOOL_INPUT = 8 * 1024        # a tool event's input larger than this goes without it
STDERR_KEEP = 64 * 1024


class RunFailed(Exception):
    """A run that ended without a result. `kind` is what clients see in the `error` event."""
    kind = "claude_exit"

    def __init__(self, detail: str, kind: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if kind:
            self.kind = kind


class SessionMissing(RunFailed):
    kind = "session_missing"


class TimedOut(RunFailed):
    kind = "timeout"


class Cancelled(RunFailed):
    kind = "cancelled"


@dataclass
class RunSpec:
    """Everything one claude run needs. Paths are absolute and already checked."""
    claude: str
    prompt: str
    cwd: Path                                    # the profile folder
    session_id: str
    resume: bool = False
    schema: dict | None = None
    system_prompt_file: Path | None = None
    add_dirs: list[tuple[Path, bool]] = field(default_factory=list)   # (folder, writable)
    builtin_tools: list[str] = field(default_factory=lambda: list(READ_TOOLS))
    mcp_config: Path | None = None
    mcp_tools: list[str] = field(default_factory=list)                # full names: mcp__<server>__<tool>
    model: str | None = None
    effort: str | None = None
    timeout: float = 900


def claude_md_excludes(*starts: Path) -> list[str]:
    """CLAUDE.md files above the run's folders (e.g. ~/CLAUDE.md, or a repo's developer notes)
    aren't for the client's prompt. Claude Code would otherwise load every ancestor's."""
    found: list[str] = []
    for start in starts:
        for parent in Path(start).parents:
            for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
                path = str(parent / name)
                if (parent / name).exists() and path not in found:
                    found.append(path)
    return found


def locked_down(starts: list[Path]) -> list[str]:
    """Flags shared by every run: only the profile's project settings, no user MCP servers,
    no ancestor CLAUDE.md files, and anything not explicitly allowed is refused, not prompted."""
    return [
        "--setting-sources", "project",
        "--settings", json.dumps({"claudeMdExcludes": claude_md_excludes(*starts)}),
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
    ]


def allowed_tools(spec: RunSpec) -> list[str]:
    """Permission rules: read tools on the profile and every added folder, write tools only on
    writable folders, Skill if asked for, and the MCP tools asked for. (`//path` is Claude
    Code's spelling of an absolute path in a rule.)"""
    tools = set(spec.builtin_tools)
    rules = ["Skill"] if "Skill" in tools else []
    folders = [(spec.cwd, False)] + list(spec.add_dirs)
    for folder, writable in folders:
        for tool in READ_TOOLS + WRITE_TOOLS:
            if tool in tools and (tool in READ_TOOLS or writable):
                rules.append(f"{tool}(/{folder}/**)")
    return rules + list(spec.mcp_tools)


def build_command(spec: RunSpec) -> list[str]:
    """The argv. The prompt isn't in it: it goes on stdin, so a prompt that starts with `-`
    can't be read as an option, and a long one isn't cut off by the kernel's argv limits."""
    cmd = [
        spec.claude, "-p",
        "--output-format", "stream-json", "--verbose",   # stream-json carries the rate-limit event
        "--include-partial-messages",                    # ...and the structured reply as it's written
    ]
    if spec.schema is not None:
        cmd += ["--json-schema", json.dumps(spec.schema)]
    if spec.system_prompt_file:
        cmd += ["--append-system-prompt-file", str(spec.system_prompt_file)]
    for folder, _ in spec.add_dirs:
        cmd += ["--add-dir", str(folder)]
    cmd += locked_down([spec.cwd] + [folder for folder, _ in spec.add_dirs])
    if spec.mcp_config:
        cmd += ["--mcp-config", str(spec.mcp_config)]
    cmd += ["--tools", ",".join(t for t in BUILTIN_TOOLS if t in spec.builtin_tools)]
    rules = allowed_tools(spec)
    if rules:
        cmd += ["--allowedTools", *rules]
    cmd += ["--resume" if spec.resume else "--session-id", spec.session_id]
    if spec.model:
        cmd += ["--model", spec.model]
    if spec.effort:
        cmd += ["--effort", spec.effort]
    return cmd


def run_env() -> dict:
    """The environment claude runs with: the broker's own, less what would let the user's
    connectors, bundled skills or memory into a client's run."""
    env = dict(os.environ)
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"     # no claude.ai connectors (mail, docs...)
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"  # only the profile's skills
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"     # the user's memory isn't the client's
    for key in [k for k in env if k.startswith("CLAUDE_BROKER_")]:
        env.pop(key)
    return env


# --- reading the output ----------------------------------------------------------------

def rate_limits(info, now: float) -> dict | None:
    """A `rate_limit_event`'s `rate_limit_info`: each window's utilisation (a fraction), reset
    time (epoch seconds) and status."""
    if not isinstance(info, dict):
        return None
    windows = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, w in unified.items():
            if isinstance(w, dict) and "utilization" in w:
                windows[name] = {"utilization": w.get("utilization"), "resets_at": w.get("resetsAt"),
                                 "status": w.get("status")}
    elif info.get("rateLimitType") and "utilization" in info:
        windows[info["rateLimitType"]] = {"utilization": info.get("utilization"),
                                          "resets_at": info.get("resetsAt"), "status": info.get("status")}
    if not windows:
        return None
    return {"windows": windows, "status": info.get("status"), "at": int(now)}


def context_size(message) -> int | None:
    """How many tokens of context an assistant message was given: its input, cached or not."""
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    total = sum(v for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                if isinstance(v := usage.get(k), int))
    return total or None


def tool_uses(message) -> list[dict]:
    """The tool calls in an assistant message, as `tool` events (StructuredOutput is the reply
    itself, not a tool the client cares about)."""
    content = message.get("content") if isinstance(message, dict) else None
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or name == "StructuredOutput":
            continue
        event: dict = {"type": "tool", "name": name}
        tool_input = block.get("input")
        if len(json.dumps(tool_input)) <= MAX_TOOL_INPUT:
            event["input"] = tool_input
        else:
            event["input_truncated"] = True
        out.append(event)
    return out


class Output:
    """What claude's stream-json lines add up to: the result message, the latest context size
    and rate limits. `feed` turns each line into the events clients see as it comes."""

    def __init__(self, fields=None, clock: Callable[[], float] | None = None):
        import time
        self.fields = fields             # a FieldStream, or None
        self.clock = clock or time.time
        self.result: dict | None = None
        self.context_tokens: int | None = None
        self.rate_limits: dict | None = None
        self.last_line = ""

    def feed(self, line: str) -> list[dict]:
        line = line.strip()
        if not line:
            return []
        self.last_line = line
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(msg, dict):
            return []
        kind = msg.get("type")
        if kind == "stream_event":
            return self.fields.feed(msg.get("event")) if self.fields else []
        if kind == "rate_limit_event":
            limits = rate_limits(msg.get("rate_limit_info"), self.clock())
            if limits:
                self.rate_limits = limits
                return [{"type": "rate_limits", "rate_limits": limits}]
            return []
        if kind == "assistant":
            self.context_tokens = context_size(msg.get("message")) or self.context_tokens
            return tool_uses(msg.get("message"))
        if kind == "result" or "is_error" in msg:
            self.result = msg
        return []


class ClaudeProcess:
    """One claude run. `run` blocks until it ends and returns its result message; `kill`
    (from another thread) ends it and everything it started."""

    def __init__(self, spec: RunSpec, on_event: Callable[[dict], None], fields=None):
        self.spec = spec
        self.on_event = on_event
        self.output = Output(fields)
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._why: RunFailed | None = None

    def kill(self, why: RunFailed | None = None) -> None:
        with self._lock:
            if self._why is None:
                self._why = why or Cancelled("cancelled")
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def run(self) -> dict:
        spec = self.spec
        with self._lock:
            if self._why:
                raise self._why
            try:
                # Its own process group, so a kill takes the MCP proxy and anything else with it.
                self.proc = subprocess.Popen(
                    build_command(spec), cwd=spec.cwd, env=run_env(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, stdin=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", start_new_session=True)
            except FileNotFoundError:
                raise RunFailed(f"claude executable not found: {spec.claude}", kind="no_claude")
        proc = self.proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stdin, stdout, stderr_pipe = proc.stdin, proc.stdout, proc.stderr
        err: list[str] = []

        def feed():
            try:
                stdin.write(spec.prompt)
            except OSError:          # claude exited without reading it all: its output says why
                pass
            finally:
                try:
                    stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        def drain():
            while chunk := stderr_pipe.read(4096):
                if sum(map(len, err)) < STDERR_KEEP:
                    err.append(chunk)

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        timer = threading.Timer(spec.timeout, self.kill,
                                args=(TimedOut(f"claude took longer than {spec.timeout:.0f}s"),))
        timer.daemon = True
        timer.start()
        try:
            for line in stdout:
                for event in self.output.feed(line):
                    self.on_event(event)
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                self.kill(RunFailed("the broker stopped reading claude"))
                proc.wait()
            feeder.join(5)
            drainer.join(5)
            stdout.close()
            stderr_pipe.close()
        if self._why:
            raise self._why
        stderr = "".join(err)
        result = self.output.result or {}
        if proc.returncode != 0 or result.get("is_error") or not result:
            detail = str(result.get("result") or stderr or self.output.last_line).strip()[:500]
            if "No conversation found" in detail or "No conversation found" in stderr:
                raise SessionMissing(detail)
            raise RunFailed(f"claude exited {proc.returncode}: {detail}")
        return result
