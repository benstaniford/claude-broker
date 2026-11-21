"""Which client started each claude session, and with which model.

A client may only resume its own sessions, with the model it started with (the prompt cache
is per model, and switching mid-session pays for the whole context again) and in the profile
it started in (claude files sessions by working folder: elsewhere it can't find them).
A session the broker never saw (its state was lost) is let through; claude itself says
whether it exists.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

MAX_SESSIONS = 5000


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text())
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        data = self._load()
        if len(data) > MAX_SESSIONS:
            for sid in sorted(data, key=lambda s: data[s].get("used", 0))[:len(data) - MAX_SESSIONS]:
                del data[sid]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)

    def check(self, session_id: str, client: str, model: str, profile: str | None = None) -> str | None:
        """Why this client can't resume this session with this model and profile, or None.
        (claude files sessions by working folder, which is the profile's.)"""
        with self._lock:
            known = self._load().get(session_id)
        if not known:
            return None
        if known.get("client") != client:
            return "that session belongs to another client"
        if (known.get("model") or "") != (model or ""):
            return (f"that session was started with model {known.get('model') or '(default)'}; "
                    f"start a new session to change model")
        if profile is not None and known.get("profile") and known["profile"] != profile:
            return (f"that session was started with profile {known['profile']}; "
                    f"resume it with the same profile")
        return None

    def record(self, session_id: str, client: str, model: str, profile: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.get(session_id) or {"client": client, "model": model or "", "profile": profile,
                                              "started": int(time.time())}
            entry["used"] = int(time.time())
            data[session_id] = entry
            self._save()
