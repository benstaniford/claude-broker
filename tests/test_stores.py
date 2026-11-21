"""Config, profiles and sessions."""

import gzip
import io
import os
import tarfile
import threading
import unittest
from pathlib import Path

from tests.helpers import BrokerCase
from claude_broker.config import (ClientConfig, ConfigError, load_broker, load_client, load_clients,
                                  write_client)
from claude_broker.profiles import DEFAULT, ProfileError, ProfileStore, content_hash, read_upload
from claude_broker.sessions import SessionStore
from claude_broker.client import pack_profile


def tarball(entries: list[tuple], gz: bool = True) -> bytes:
    """entries: (name, content) for files, (name, None, type) for others."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as archive:
        for entry in entries:
            name, content = entry[0], entry[1]
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = entry[2]
                if entry[2] == tarfile.SYMTYPE:
                    info.linkname = entry[3] if len(entry) > 3 else "/etc/passwd"
                archive.addfile(info)
            else:
                data = content.encode() if isinstance(content, str) else content
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = [("SYSTEM.md", "You are a helper."), ("CLAUDE.md", "# notes"),
        (".claude", None, tarfile.DIRTYPE), (".claude/skills", None, tarfile.DIRTYPE),
        (".claude/skills/rules/SKILL.md", "---\nname: rules\n---\n"),
        (".claude/skills/rules/ref/table.csv", "a,b\n")]


class ConfigTest(BrokerCase):
    def test_loads_clients_and_skips_bad_ones(self):
        self.write_client("bad", "max_concurrent = 0\n")
        self.write_client("Bad Name", "")
        self.write_client("odd", "surprise = 1\n")
        clients, problems = load_clients()
        self.assertEqual(list(clients), ["app"])
        self.assertEqual(len(problems), 3)
        app = clients["app"]
        self.assertEqual(app.workspace_roots, [self.work.resolve()])
        self.assertEqual(app.broker_socket(), self.tmp / "run" / "app" / "broker.sock")

    def test_most_specific_root_wins(self):
        client = ClientConfig("x", workspace_roots=[Path("/data")], read_only_roots=[Path("/data/books")])
        self.assertEqual(client.root_for(Path("/data/games/1")), (Path("/data"), True))
        self.assertEqual(client.root_for(Path("/data/books/a")), (Path("/data/books"), False))
        self.assertIsNone(client.root_for(Path("/database")))
        self.assertIsNone(client.root_for(Path("/")))

    def test_broker_settings(self):
        self.assertEqual(load_broker().claude, str(self.claude))
        self.write_broker("log_level = \"loud\"\n")
        with self.assertRaises(ConfigError):
            load_broker()
        os.remove(self.config / "broker.toml")
        self.assertEqual(load_broker().max_concurrent, 2)

    def test_write_reads_back(self):
        client = ClientConfig("game-2", workspace_roots=[self.work], read_only_roots=[self.books],
                              models=["m1", "m2"], default_model="m2", max_concurrent=3,
                              allowed_builtin_tools=["Read", "Write"])
        path = write_client(client)
        again = load_client(path)
        self.assertEqual((again.models, again.default_model, again.max_concurrent, again.allowed_builtin_tools),
                         (["m1", "m2"], "m2", 3, ["Read", "Write"]))
        self.write_client("tools", 'allowed_builtin_tools = ["Bash"]\n')
        with self.assertRaises(ConfigError):
            load_client(self.config / "clients.d" / "tools.toml")


class ProfileTest(BrokerCase):
    def setUp(self):
        super().setUp()
        self.store = ProfileStore(self.tmp / "state" / "profiles")

    def test_upload_unpacks_text_without_exec_bits(self):
        profile = self.store.put("app", "helper", tarball(GOOD))
        self.assertEqual(profile.files, 4)
        self.assertEqual((profile.path / "SYSTEM.md").read_text(), "You are a helper.")
        self.assertEqual(profile.system_prompt, profile.path / "SYSTEM.md")
        mode = (profile.path / ".claude/skills/rules/SKILL.md").stat().st_mode
        self.assertFalse(mode & 0o111)
        self.assertEqual(self.store.get("app", "helper").sha256, profile.sha256)
        self.assertIsNone(self.store.get("other", "helper"))                 # per client
        self.assertEqual([p.name for p in self.store.list("app")], ["helper"])

    def test_same_content_same_hash_however_packed(self):
        a = self.store.put("app", "h", tarball(GOOD))
        b = self.store.put("app", "h", tarball(list(reversed(GOOD)), gz=False))
        self.assertEqual(a.sha256, b.sha256)
        folder = self.tmp / "prof"
        (folder / ".claude/skills/s").mkdir(parents=True)
        (folder / "SYSTEM.md").write_text("x")
        (folder / ".claude/skills/s/SKILL.md").write_text("y")
        self.assertEqual(pack_profile(folder), pack_profile(folder))          # deterministic
        self.assertEqual(self.store.put("app", "p", pack_profile(folder)).files, 2)

    def test_hostile_uploads(self):
        cases = {
            "settings": [(".claude/settings.json", '{"hooks": {}}')],
            "script": [(".claude/skills/x/run.py", "import os")],
            "traversal": [("../../evil.md", "x")],
            "absolute": [("/etc/evil.md", "x")],
            "symlink": [(".claude/skills/x/link.md", None, tarfile.SYMTYPE)],
            "hardlink": [(".claude/skills/x/hard.md", None, tarfile.LNKTYPE, "SYSTEM.md")],
            "device": [("SYSTEM.md", None, tarfile.CHRTYPE)],
            "binary": [(".claude/skills/x/a.md", b"\xff\xfe\x00")],
            "random file": [("notes.md", "x")],
            "skill folder top": [(".claude/skills/README.md", "x")],
            "mcp": [(".mcp.json", "{}")],
        }
        for name, entries in cases.items():
            with self.subTest(name), self.assertRaises(ProfileError):
                self.store.put("app", "h", tarball(entries))
        with self.assertRaises(ProfileError):
            self.store.put("app", "h", b"not a tarball")
        with self.assertRaises(ProfileError) as caught:                 # a small gzip of a huge archive
            self.store.put("app", "h", gzip.compress(b"\0" * (64 * 1024 * 1024)))
        self.assertIn("uncompressed", caught.exception.errors[0])
        with self.assertRaises(ProfileError):
            self.store.put("app", "h", tarball(GOOD)[:-20])             # cut short
        with self.assertRaises(ProfileError):
            self.store.put("app", "Bad/Name", tarball(GOOD))
        with self.assertRaises(ProfileError):
            self.store.put("app", DEFAULT, tarball(GOOD))
        self.assertIsNone(self.store.get("app", "h"))

    def test_swap_keeps_the_path_and_spares_a_running_copy(self):
        first = self.store.put("app", "h", tarball([("SYSTEM.md", "one")]))
        with self.store.using("app", "h"):
            second = self.store.put("app", "h", tarball([("SYSTEM.md", "two")]))
            self.assertEqual(first.path, second.path)
            self.assertEqual((second.path / "SYSTEM.md").read_text(), "two")
            old = [p for p in second.path.parent.iterdir() if ".old-" in p.name]
            self.assertEqual(len(old), 1)                                    # kept while in use
            self.assertEqual((old[0] / "SYSTEM.md").read_text(), "one")
        self.assertFalse([p for p in second.path.parent.iterdir() if ".old-" in p.name])
        self.store.put("app", "h", tarball([("SYSTEM.md", "three")]))
        self.assertFalse([p for p in second.path.parent.iterdir() if ".old-" in p.name])

    def test_delete_and_default(self):
        self.store.put("app", "h", tarball(GOOD))
        self.assertTrue(self.store.delete("app", "h"))
        self.assertIsNone(self.store.get("app", "h"))
        self.assertFalse(self.store.delete("app", "h"))
        default = self.store.get("app", DEFAULT)
        self.assertTrue(default.path.is_dir())
        self.assertEqual(self.store.list("app"), [])

    def test_sweep_clears_leftovers(self):
        folder = self.tmp / "state" / "profiles" / "app"
        (folder / ".h.new-1234abcd").mkdir(parents=True)
        (folder / ".h.old-1234abcd").mkdir()
        self.store.sweep()
        self.assertEqual(list(folder.iterdir()), [])

    def test_read_upload_and_hash(self):
        files = read_upload(tarball(GOOD))
        self.assertEqual(sorted(files), [".claude/skills/rules/SKILL.md", ".claude/skills/rules/ref/table.csv",
                                         "CLAUDE.md", "SYSTEM.md"])
        self.assertNotEqual(content_hash(files), content_hash({**files, "CLAUDE.md": b"changed"}))


class SessionTest(BrokerCase):
    def test_owner_and_model(self):
        store = SessionStore(self.tmp / "state" / "sessions.json")
        self.assertIsNone(store.check("s1", "app", "m"))                 # never seen: claude decides
        store.record("s1", "app", "m", "helper")
        self.assertIsNone(store.check("s1", "app", "m"))
        self.assertIn("another client", store.check("s1", "other", "m"))
        self.assertIn("model", store.check("s1", "app", "n"))
        self.assertIsNone(store.check("s1", "app", "m", "helper"))
        self.assertIn("profile helper", store.check("s1", "app", "m", "_default"))
        again = SessionStore(self.tmp / "state" / "sessions.json")       # it lasts
        self.assertIn("model", again.check("s1", "app", ""))

    def test_concurrent_records(self):
        store = SessionStore(self.tmp / "state" / "sessions.json")
        threads = [threading.Thread(target=store.record, args=(f"s{i}", "app", "", "p")) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(SessionStore(self.tmp / "state" / "sessions.json")._load()), 20)


if __name__ == "__main__":
    unittest.main()
