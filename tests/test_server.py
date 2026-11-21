"""The broker end to end: real unix sockets, the client library, the fake claude."""

import json
import os
import socket
import stat
import threading
import time
import unittest

from tests.helpers import BrokerCase
from tests.test_stores import GOOD, tarball
from claude_broker.client import BrokerError

SCHEMA = {"type": "object", "properties": {"narration": {"type": "string"}}}


def kinds(events):
    return [e["type"] for e in events]


class ServerTest(BrokerCase):
    def setUp(self):
        super().setUp()
        self.start_broker()
        self.app = self.client()

    # --- the basics -----------------------------------------------------------------------

    def test_health_and_socket_permissions(self):
        health = self.app.health()
        self.assertEqual((health["client"], health["claude"], health["tools_socket"]), ("app", str(self.claude), False))
        self.assertEqual(stat.S_IMODE(self.socket().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.socket().parent.stat().st_mode), 0o700)
        with self.assertRaises(BrokerError) as caught:
            self.app._json("GET", "/v2/health")
        self.assertEqual(caught.exception.status, 404)

    def test_a_run_streams_then_resumes(self):
        events = list(self.app.run("Begin.", schema=SCHEMA, stream_fields=["narration"],
                                   add_dirs=[{"path": str(self.work), "write": True}, str(self.books)]))
        self.assertEqual(kinds(events)[0], "started")
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        narration = "".join(e.get("delta", "") for e in events if e["type"] == "field")
        self.assertEqual(narration, "Arrows hiss from the brush.")
        self.assertIn({"type": "field_closed", "name": "narration", "text": narration},
                      [{k: v for k, v in e.items() if k != "seq"} for e in events])
        result = events[-1]
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["structured"]["narration"], narration)
        self.assertEqual(result["context_tokens"], 1240)
        self.assertEqual(result["session_id"], events[0]["session_id"])
        args = self.calls()[-1]["args"]
        self.assertIn(f"Write(/{self.work}/**)", args)
        self.assertIn(f"Read(/{self.books}/**)", args)
        self.assertNotIn(f"Write(/{self.books}/**)", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")   # default with a writable dir
        self.assertTrue(self.calls()[-1]["cwd"].endswith("/profiles/app/_default"))

        again = self.app.result("Go on.", session={"resume": result["session_id"]})
        self.assertEqual(again["session_id"], result["session_id"])
        args = self.calls()[-1]["args"]
        self.assertEqual(args[args.index("--resume") + 1], result["session_id"])
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")

    def test_errors_come_as_events(self):
        os.environ["FAKE_CLAUDE_MODE"] = "noschema"
        with self.assertRaises(BrokerError) as caught:
            self.app.result("x", schema=SCHEMA)
        self.assertEqual(caught.exception.kind, "schema")
        os.environ["FAKE_CLAUDE_MODE"] = "missing"
        with self.assertRaises(BrokerError) as caught:
            self.app.result("x", session={"resume": "0b7f8a4e-3c55-4f43-9a9b-2b0b6f1d1e11"})
        self.assertEqual(caught.exception.kind, "session_missing")

    def test_bad_requests(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        cases = [
            dict(add_dirs=[str(outside)]),
            dict(add_dirs=[{"path": str(self.books), "write": True}]),
            dict(add_dirs=["relative/path"]),
            dict(add_dirs=[str(self.work / "missing")]),
            dict(builtin_tools=["Bash"]),
            dict(mcp_tools=["roll"]),                             # no tools.sock yet
            dict(profile="nope"),
            dict(effort="extreme"),
            dict(timeout=10_000),
            dict(session={"resume": "not-a-uuid"}),
            dict(run_id="has spaces"),
            dict(schema={"description": "x" * 70_000}),
        ]
        for kw in cases:
            with self.subTest(kw), self.assertRaises(BrokerError) as caught:
                list(self.app.run("x", **kw))
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(BrokerError):
            list(self.app._events("POST", "/v1/runs", b"[1, 2]"))
        self.assertEqual(self.calls(), [])

    def test_a_symlink_out_of_a_root_is_refused(self):
        outside = self.tmp / "secret"
        outside.mkdir()
        (self.work / "sneaky").symlink_to(outside)
        with self.assertRaises(BrokerError) as caught:
            list(self.app.run("x", add_dirs=[str(self.work / "sneaky")]))
        self.assertIn("outside", caught.exception.errors[0])

    def test_models_and_sessions_per_client(self):
        self.write_client("app", f'workspace_roots = ["{self.work}"]\nmodels = ["m1", "m2"]\n'
                                 f'default_model = "m1"\n')
        self.write_client("other", f'workspace_roots = ["{self.work}"]\n')
        self.broker.reload()
        with self.assertRaises(BrokerError):
            list(self.app.run("x", model="m3"))
        first = self.app.result("x")
        args = self.calls()[-1]["args"]
        self.assertEqual(args[args.index("--model") + 1], "m1")
        with self.assertRaises(BrokerError) as caught:
            list(self.app.run("x", model="m2", session={"resume": first["session_id"]}))
        self.assertIn("model", caught.exception.errors[0])
        with self.assertRaises(BrokerError) as caught:
            list(self.client("other").run("x", session={"resume": first["session_id"]}))
        self.assertIn("another client", caught.exception.errors[0])

    # --- profiles -------------------------------------------------------------------------------

    def test_profiles_over_the_socket(self):
        put = self.app.put_profile("helper", tarball(GOOD))
        self.assertEqual(put["files"], 4)
        self.assertEqual([p["name"] for p in self.app.profiles()], ["helper"])
        self.app.result("x", profile="helper")
        call = self.calls()[-1]
        self.assertTrue(call["cwd"].endswith("/profiles/app/helper"))
        self.assertIn("--append-system-prompt-file", call["args"])
        first = self.app.result("x", profile="helper")
        with self.assertRaises(BrokerError) as caught:                  # claude couldn't find it elsewhere
            list(self.app.run("x", session={"resume": first["session_id"]}))
        self.assertIn("profile helper", caught.exception.errors[0])
        self.app.result("x", profile="helper", session={"resume": first["session_id"]})
        with self.assertRaises(BrokerError) as caught:
            self.app.put_profile("bad", tarball([(".claude/settings.json", "{}")]))
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.client("app").delete_profile("helper"), {"deleted": "helper"})
        with self.assertRaises(BrokerError):
            self.app.delete_profile("helper")

    # --- run ids, rejoining, cancelling, the queue --------------------------------------------

    def test_same_run_id_runs_once(self):
        first = list(self.app.run("x", run_id="turn-1", schema=SCHEMA))
        second = list(self.app.run("ignored", run_id="turn-1", schema=SCHEMA))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls()), 1)
        tail = list(self.app.follow("turn-1", start=len(first) - 1))
        self.assertEqual(tail, [first[-1]])
        self.assertEqual([r["run_id"] for r in self.app.runs()], ["turn-1"])
        self.assertEqual(self.app.runs()[0]["outcome"], "result")

    def test_run_ids_are_unquoted(self):
        self.app.result("x", run_id="job:1")                         # the client sends job%3A1
        self.assertEqual(next(self.app.follow("job:1"))["type"], "started")
        self.assertEqual(self.app.cancel("job:1")["run_id"], "job:1")

    def test_leaving_does_not_stop_a_run(self):
        os.environ["FAKE_SLEEP"] = "1"
        events = self.app.run("x", run_id="r1", schema=SCHEMA)
        self.assertEqual(next(events)["type"], "started")
        events.close()                                            # the app restarts mid-run
        rest = list(self.client().follow("r1"))
        self.assertEqual(rest[-1]["type"], "result")
        self.assertEqual(len(self.calls()), 1)

    def test_cancel(self):
        os.environ["FAKE_CLAUDE_MODE"] = "hang"
        got = []
        reader = threading.Thread(target=lambda: got.extend(self.app.run("x", run_id="slow")))
        reader.start()
        self.wait_for(lambda: self.calls(), message="claude to start")
        summary = self.app.cancel("slow")
        self.assertEqual(summary["run_id"], "slow")
        reader.join(10)
        self.assertEqual(got[-1]["type"], "error")
        self.assertEqual(got[-1]["kind"], "cancelled")
        with self.assertRaises(BrokerError):
            self.app.cancel("never")

    def test_queue_per_client_and_cancel_while_queued(self):
        os.environ["FAKE_SLEEP"] = "1"
        results = {}

        def go(run_id):
            results[run_id] = list(self.client().run("x", run_id=run_id))

        threads = [threading.Thread(target=go, args=(f"q{i}",)) for i in range(3)]
        for t in threads:
            t.start()
            time.sleep(0.2)
        self.wait_for(lambda: len(self.app.runs()) == 3, message="three runs")
        self.app.cancel("q2")
        for t in threads:
            t.join(20)
        self.assertEqual(kinds(results["q0"])[0], "started")              # app's cap is 1
        self.assertEqual(results["q1"][0], {"seq": 0, "type": "queued", "position": 1})
        self.assertEqual(results["q1"][-1]["type"], "result")
        self.assertEqual(results["q2"][-1]["kind"], "cancelled")
        self.assertNotIn("started", kinds(results["q2"]))
        self.assertEqual(len(self.calls()), 2)

    def test_global_cap_across_clients(self):
        (self.config / "broker.toml").write_text(f'claude = "{self.claude}"\nmax_concurrent = 1\n')
        self.write_client("other", f'workspace_roots = ["{self.work}"]\nmax_concurrent = 4\n')
        self.broker.reload()
        os.environ["FAKE_SLEEP"] = "0.5"
        out = {}
        a = threading.Thread(target=lambda: out.setdefault("a", list(self.app.run("x"))))
        b = threading.Thread(target=lambda: out.setdefault("b", list(self.client("other").run("x"))))
        a.start()
        self.wait_for(lambda: self.calls(), message="the first run")
        b.start()
        a.join(10)
        b.join(10)
        self.assertEqual(out["b"][0]["type"], "queued")
        self.assertEqual(out["b"][-1]["type"], "result")

    def test_finished_runs_are_forgotten(self):
        (self.config / "broker.toml").write_text(f'claude = "{self.claude}"\nkeep_finished_seconds = 0\n')
        self.broker.reload()
        self.app.result("x", run_id="gone")
        self.broker.registry.reap()
        with self.assertRaises(BrokerError) as caught:
            list(self.app.follow("gone"))
        self.assertEqual(caught.exception.status, 404)

    def test_usage(self):
        self.assertIsNone(self.app.usage())
        os.environ["FAKE_RATE_LIMITS"] = json.dumps({"rateLimitType": "five_hour", "utilization": 0.3})
        result = self.app.result("x")
        self.assertEqual(result["rate_limits"]["windows"]["five_hour"]["utilization"], 0.3)
        self.assertEqual(self.app.usage()["windows"]["five_hour"]["utilization"], 0.3)

    # --- config reloads and restarts -------------------------------------------------------------

    def test_reload_adds_and_removes_clients(self):
        self.write_client("second", f'workspace_roots = ["{self.work}"]\n')
        self.broker.reload()
        self.assertEqual(self.client("second").health()["client"], "second")
        os.remove(self.config / "clients.d" / "second.toml")
        self.broker.reload()
        self.assertFalse(self.socket("second").exists())
        self.assertTrue(self.socket("second").parent.is_dir())      # the mounted folder stays
        with self.assertRaises(OSError):
            self.client("second").health()

    def test_restart_replaces_a_stale_socket(self):
        self.broker.close()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.socket()))                              # a socket nobody listens on
        stale.close()
        self.start_broker()
        self.assertEqual(self.client().health()["client"], "app")
        self.broker.close()
        self.assertFalse(self.socket().exists())                    # removed on close
        self.socket().write_text("not a socket")
        from claude_broker.server import Broker
        broker = Broker()
        problems = broker.reload()
        self.addCleanup(broker.close)
        self.assertIn("isn't a socket", problems[0])


class McpTest(BrokerCase):
    """A run's MCP tools reach the client's server on tools.sock through `mcp-proxy`."""

    def setUp(self):
        super().setUp()
        self.start_broker()
        tools = self.tmp / "run" / "app" / "tools.sock"
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(tools))
        self.server.listen(1)
        self.addCleanup(self.server.close)

        def serve():
            conn, _ = self.server.accept()
            with conn:
                request = json.loads(conn.makefile().readline())
                conn.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                          "result": {"echo": request["params"]}}) + "\n").encode())

        threading.Thread(target=serve, daemon=True).start()

    def test_tool_call_round_trip(self):
        os.environ["FAKE_MCP_CALL"] = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                  "params": {"name": "roll", "arguments": {"dice": "d20"}}})
        result = self.client().result("roll", mcp_tools=["roll", "monster_show"])
        reply = json.loads(result["text"])
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"]["echo"]["arguments"], {"dice": "d20"})
        args = self.calls()[-1]["args"]
        self.assertIn("mcp__app__roll", args)
        self.assertIn("mcp__app__monster_show", args)
        self.assertIn("--strict-mcp-config", args)
        config_path = args[args.index("--mcp-config") + 1]
        self.assertFalse(os.path.exists(config_path))               # removed after the run

    def test_proxy_without_a_server(self):
        from claude_broker.mcp_proxy import relay
        self.assertEqual(relay(str(self.tmp / "nothing.sock")), 1)

    def test_a_linked_tools_socket_is_refused(self):
        from claude_broker.mcp_proxy import relay
        host = socket.socket(socket.AF_UNIX)                         # some other socket on the host
        host.bind(str(self.tmp / "host.sock"))
        host.listen(1)
        self.addCleanup(host.close)
        link = self.tmp / "link.sock"
        link.symlink_to(self.tmp / "host.sock")
        self.assertEqual(relay(str(link)), 1)
        tools = self.tmp / "run" / "app" / "tools.sock"
        tools.unlink()
        tools.symlink_to(self.tmp / "host.sock")
        self.assertFalse(self.client().health()["tools_socket"])
        with self.assertRaises(BrokerError) as caught:
            list(self.client().run("x", mcp_tools=["roll"]))
        self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
