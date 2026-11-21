import json
import os
import threading
import time
import unittest
from pathlib import Path

from tests.helpers import BrokerCase
from claude_broker.claude import (Cancelled, ClaudeProcess, Output, RunFailed, RunSpec, SessionMissing, TimedOut,
                                  allowed_tools, build_command, claude_md_excludes, rate_limits)
from claude_broker.fields import FieldStream


class CommandTest(unittest.TestCase):
    def spec(self, **kw):
        base = dict(claude="claude", prompt="hi", cwd=Path("/p/prof"), session_id="s-1",
                    add_dirs=[(Path("/w/game"), True), (Path("/b/book"), False)],
                    builtin_tools=["Read", "Grep", "Edit", "Write", "Skill"])
        base.update(kw)
        return RunSpec(**base)

    def test_rules_follow_folders_and_tools(self):
        rules = allowed_tools(self.spec(mcp_tools=["mcp__app__roll"]))
        self.assertIn("Skill", rules)
        for folder in ("/p/prof", "/w/game", "/b/book"):
            self.assertIn(f"Read(//{folder[1:]}/**)", rules)
            self.assertIn(f"Grep(//{folder[1:]}/**)", rules)
            self.assertNotIn(f"Glob(//{folder[1:]}/**)", rules)        # not asked for
        self.assertIn("Edit(//w/game/**)", rules)
        self.assertIn("Write(//w/game/**)", rules)
        self.assertNotIn("Edit(//b/book/**)", rules)                   # read-only
        self.assertNotIn("Write(//p/prof/**)", rules)                  # the profile is never written
        self.assertEqual(rules[-1], "mcp__app__roll")

    def test_command(self):
        cmd = build_command(self.spec(schema={"type": "object"}, model="m", effort="high", resume=True,
                                      system_prompt_file=Path("/p/prof/SYSTEM.md"), mcp_config=Path("/r/m.json")))
        joined = " ".join(cmd)
        self.assertEqual(cmd[:3], ["claude", "-p", "--output-format"])
        self.assertNotIn("hi", cmd)                                     # the prompt goes on stdin
        for flag in ("--output-format stream-json", "--include-partial-messages", "--strict-mcp-config",
                     "--permission-mode dontAsk", "--setting-sources project", "--resume s-1", "--model m",
                     "--effort high", "--append-system-prompt-file /p/prof/SYSTEM.md", "--mcp-config /r/m.json",
                     "--add-dir /w/game", "--add-dir /b/book"):
            self.assertIn(flag, joined)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Edit,Write,Skill")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), {"type": "object"})
        self.assertNotIn("Bash", joined)

    def test_new_session_and_no_tools(self):
        cmd = build_command(self.spec(builtin_tools=[], add_dirs=[]))
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", cmd)
        self.assertNotIn("--json-schema", cmd)

    def test_claude_md_excludes_ancestors(self):
        found = claude_md_excludes(Path.home() / "nowhere" / "deeper")
        self.assertTrue(all(Path(p).name in ("CLAUDE.md", "CLAUDE.local.md") for p in found))


class OutputTest(unittest.TestCase):
    def test_lines_become_events(self):
        out = Output(FieldStream(["a"]), clock=lambda: 1000)
        self.assertEqual(out.feed("not json\n"), [])
        events = out.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 5, "status": "ok"}}}}))
        self.assertEqual(events[0]["rate_limits"]["windows"]["five_hour"]["utilization"], 0.4)
        self.assertEqual(events[0]["rate_limits"]["at"], 1000)
        big = "x" * 9000
        events = out.feed(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5,
                                      "cache_read_input_tokens": 100}, "content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}},
            {"type": "tool_use", "name": "Write", "input": {"content": big}},
            {"type": "tool_use", "name": "StructuredOutput", "input": {}}]}}))
        self.assertEqual(events, [{"type": "tool", "name": "Read", "input": {"file_path": "/a"}},
                                  {"type": "tool", "name": "Write", "input_truncated": True}])
        self.assertEqual(out.context_tokens, 105)
        out.feed(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
        self.assertEqual(out.result["result"], "ok")

    def test_single_window_rate_limit(self):
        limits = rate_limits({"rateLimitType": "seven_day", "utilization": 0.9, "resetsAt": 9, "status": "warn"}, 1)
        self.assertEqual(list(limits["windows"]), ["seven_day"])
        self.assertIsNone(rate_limits({"status": "ok"}, 1))


class ProcessTest(BrokerCase):
    def spec(self, **kw):
        base = dict(claude=str(self.claude), prompt="hi", cwd=self.work, session_id="abc",
                    schema={"type": "object"}, timeout=20)
        base.update(kw)
        return RunSpec(**base)

    def run_spec(self, spec, fields=("narration",)):
        events = []
        process = ClaudeProcess(spec, events.append, FieldStream(list(fields)))
        return process, process.run(), events

    def test_a_run(self):
        os.environ["FAKE_RATE_LIMITS"] = json.dumps({"rateLimitType": "five_hour", "utilization": 0.2})
        process, result, events = self.run_spec(self.spec())
        self.assertEqual(result["structured_output"]["narration"], "Arrows hiss from the brush.")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "rate_limits")
        self.assertIn("field", kinds)
        self.assertIn("field_closed", kinds)
        self.assertIn({"type": "tool", "name": "Read", "input": {"file_path": "/x/notes.md"}}, events)
        self.assertEqual(process.output.context_tokens, 1240)
        call = self.calls()[-1]
        self.assertEqual(call["cwd"], str(self.work))
        self.assertEqual(call["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
        self.assertEqual(call["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertIsNone(call["env"]["CLAUDE_BROKER_STATE"])              # the broker's own settings stay home
        self.assertEqual(call["prompt"], "hi")

    def test_prompt_is_never_an_option(self):
        prompt = "--dangerously-skip-permissions " + "x" * 600_000      # past the kernel's per-argument limit
        self.run_spec(self.spec(prompt=prompt))
        call = self.calls()[-1]
        self.assertEqual(call["prompt"], prompt)
        self.assertFalse(any(a.startswith("--dangerously") for a in call["args"]))

    def test_failures(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        with self.assertRaises(RunFailed) as caught:
            self.run_spec(self.spec())
        self.assertIn("boom", caught.exception.detail)
        self.assertEqual(caught.exception.kind, "claude_exit")
        os.environ["FAKE_CLAUDE_MODE"] = "missing"
        with self.assertRaises(SessionMissing):
            self.run_spec(self.spec(resume=True))
        os.environ["FAKE_CLAUDE_MODE"] = "crash-after-stream"
        with self.assertRaises(RunFailed):
            self.run_spec(self.spec())
        with self.assertRaises(RunFailed) as caught:
            self.run_spec(self.spec(claude=str(self.tmp / "no-such-claude")))
        self.assertEqual(caught.exception.kind, "no_claude")

    def test_timeout_and_kill(self):
        os.environ["FAKE_CLAUDE_MODE"] = "hang"
        began = time.monotonic()
        with self.assertRaises(TimedOut):
            self.run_spec(self.spec(timeout=0.5))
        self.assertLess(time.monotonic() - began, 10)
        process = ClaudeProcess(self.spec(), lambda e: None)
        threading.Timer(0.5, process.kill).start()
        with self.assertRaises(Cancelled):
            process.run()
        process = ClaudeProcess(self.spec(), lambda e: None)
        process.kill()                                                   # before it starts
        with self.assertRaises(Cancelled):
            process.run()


if __name__ == "__main__":
    unittest.main()
