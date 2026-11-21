"""A broker in a temporary folder, with the fake claude."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_ENV = ("FAKE_CLAUDE_MODE", "FAKE_SLEEP", "FAKE_REPLY", "FAKE_RATE_LIMITS", "FAKE_RETRY", "FAKE_MCP_CALL")


def short_tmp(prefix: str) -> Path:
    """Unix socket paths must be short (108 bytes), so tests live in /tmp."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


class BrokerCase(unittest.TestCase):
    """Sets CLAUDE_BROKER_* to a temp folder, writes a fake claude, and (with `start_broker`)
    runs a Broker with one client, `app`, whose roots are `self.work` and `self.books`."""

    def setUp(self):
        self.tmp = short_tmp("cb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_before = {k: os.environ.get(k) for k in
                           ("CLAUDE_BROKER_CONFIG", "CLAUDE_BROKER_STATE", "CLAUDE_BROKER_RUNTIME",
                            "FAKE_CLAUDE_LOG", *FAKE_ENV)}
        self.addCleanup(self._restore_env)
        for key in FAKE_ENV:
            os.environ.pop(key, None)
        os.environ["CLAUDE_BROKER_CONFIG"] = str(self.tmp / "config")
        os.environ["CLAUDE_BROKER_STATE"] = str(self.tmp / "state")
        os.environ["CLAUDE_BROKER_RUNTIME"] = str(self.tmp / "run")
        self.log = self.tmp / "claude.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.claude = self.tmp / "claude"
        self.claude.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_claude.py'} \"$@\"\n")
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.work = self.tmp / "work"
        self.books = self.tmp / "books"
        self.work.mkdir()
        self.books.mkdir()
        self.config = self.tmp / "config"
        (self.config / "clients.d").mkdir(parents=True)
        self.write_broker()
        self.write_client("app", f'workspace_roots = ["{self.work}"]\nread_only_roots = ["{self.books}"]\n')

    def _restore_env(self):
        for key, value in self.env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_broker(self, extra: str = "") -> None:
        (self.config / "broker.toml").write_text(f'claude = "{self.claude}"\nmax_concurrent = 2\n{extra}')

    def write_client(self, name: str, text: str) -> None:
        (self.config / "clients.d" / f"{name}.toml").write_text(text)

    def start_broker(self):
        from claude_broker.server import Broker
        self.broker = Broker()
        self.broker.start()
        self.addCleanup(self.broker.close)
        return self.broker

    def socket(self, client: str = "app") -> Path:
        return self.tmp / "run" / client / "broker.sock"

    def client(self, name: str = "app", timeout: float | None = 30):
        from claude_broker.client import BrokerClient
        return BrokerClient(self.socket(name), timeout=timeout)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def wait_for(self, test, timeout: float = 10, message: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = test()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {message}")
