"""The client library against the fake broker (what an app's own tests will use), and the CLI."""

import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import BrokerCase, short_tmp
from claude_broker import VERSION, cli
from claude_broker.client import BrokerClient, BrokerError, final
from claude_broker.config import load_client
from claude_broker.fake_broker import FakeBroker, FakeHTTPError


class FakeBrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tmp("cbf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeBroker(self.tmp / "broker.sock").start()
        self.addCleanup(self.fake.stop)
        self.client = BrokerClient(self.tmp / "broker.sock", timeout=10)

    def test_scripted_runs(self):
        self.fake.reply = lambda body: [{"type": "field", "name": "a", "delta": "hel"},
                                        {"type": "field", "name": "a", "delta": "lo"},
                                        {"type": "result", "structured": {"a": "hello"}}]
        events = list(self.client.run("hi", run_id="t1", stream_fields=["a"]))
        self.assertEqual([e["type"] for e in events], ["started", "field", "field", "result"])
        self.assertEqual(events[-1]["session_id"], events[0]["session_id"])
        self.assertEqual(self.fake.requests[-1]["body"], {"prompt": "hi", "run_id": "t1", "stream_fields": ["a"]})
        self.assertEqual(list(self.client.follow("t1", start=3)), events[3:])
        self.assertEqual(self.client.cancel("t1")["run_id"], "t1")
        self.assertEqual(self.fake.cancelled, ["t1"])

    def test_errors(self):
        self.fake.reply = lambda body: [{"type": "error", "kind": "timeout", "detail": "too slow"}]
        with self.assertRaises(BrokerError) as caught:
            self.client.result("hi")
        self.assertEqual(caught.exception.kind, "timeout")

        def refuse(body):
            raise FakeHTTPError(400, "add_dirs: outside")
        self.fake.reply = refuse
        with self.assertRaises(BrokerError) as caught:
            list(self.client.run("hi"))
        self.assertEqual((caught.exception.status, caught.exception.errors), (400, ["add_dirs: outside"]))
        with self.assertRaises(BrokerError):
            final(iter([{"type": "started"}]))

    def test_profiles_and_health(self):
        folder = self.tmp / "profile"
        folder.mkdir()
        (folder / "SYSTEM.md").write_text("hi")
        put = self.client.put_profile("helper", folder)
        self.assertEqual([p["name"] for p in self.client.profiles()], ["helper"])
        self.assertEqual(put["sha256"], self.client.profiles()[0]["sha256"])
        self.assertTrue(self.client.health()["ok"])
        self.fake.rate_limits = {"windows": {}}
        self.assertEqual(self.client.usage(), {"windows": {}})


class CliTest(BrokerCase):
    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_reload_service"):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        self.assertEqual(self.run_cli("version")[1].strip(), VERSION)

    def test_client_add_list_remove(self):
        code, out, _ = self.run_cli("client", "add", "demo", "--root", str(self.work), "--read-only",
                                    str(self.books), "--model", "m1", "--max-concurrent", "2")
        self.assertEqual(code, 0)
        self.assertIn("/run/claude-broker", out)
        client = load_client(self.config / "clients.d" / "demo.toml")
        self.assertEqual((client.workspace_roots, client.read_only_roots, client.models, client.max_concurrent),
                         ([self.work.resolve()], [self.books.resolve()], ["m1"], 2))
        self.assertEqual(self.run_cli("client", "add", "demo", "--root", str(self.work))[0], 1)   # exists
        self.assertEqual(self.run_cli("client", "add", "demo", "--root", str(self.work), "--force")[0], 0)
        self.assertEqual(self.run_cli("client", "add", "Bad Name")[0], 2)
        self.assertEqual(self.run_cli("client", "add", "t", "--tools", "Read,Bash")[0], 2)
        self.assertFalse((self.config / "clients.d" / "t.toml").exists())
        code, out, _ = self.run_cli("client", "list")
        self.assertIn("demo", out)
        self.assertEqual(self.run_cli("client", "remove", "demo")[0], 0)
        self.assertEqual(self.run_cli("client", "remove", "demo")[0], 1)

    def test_call_and_profile(self):
        self.start_broker()
        profile = self.tmp / "prof"
        profile.mkdir()
        (profile / "SYSTEM.md").write_text("Be brief.")
        code, out, _ = self.run_cli("profile", "put", "--client", "app", "brief", str(profile))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["files"], 1)
        code, out, _ = self.run_cli("profile", "list", "--client", "app")
        self.assertIn("brief", out)
        code, out, _ = self.run_cli("call", "--client", "app", "hello", "--profile", "brief", "--raw",
                                    "--schema", '{"type": "object"}', "--field", "narration")
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(events[-1]["type"], "result")
        code, out, err = self.run_cli("call", "--client", "app", "hello", "--schema", '{"type": "object"}',
                                      "--field", "narration")
        self.assertEqual(code, 0)
        self.assertIn("Arrows hiss from the brush.", err)
        self.assertEqual(json.loads(out)["type"], "result")
        code, _, err = self.run_cli("call", "--client", "nobody", "hello")
        self.assertEqual(code, 1)
        self.assertIn("can't reach", err)


if __name__ == "__main__":
    unittest.main()
