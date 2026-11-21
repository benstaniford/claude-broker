"""Profiles: a client's Claude workspace, uploaded as text and checked.

A profile is the folder a run starts in (its `cwd`):

    SYSTEM.md              appended to Claude's system prompt (optional)
    CLAUDE.md              project memory (optional)
    .claude/skills/**      skills: text only (.md .txt .json .csv .yaml .yml)

Nothing else is accepted: no settings, hooks, MCP servers, scripts or links. Execute bits
are dropped. The broker never runs anything a client sends.

Each profile lives at a fixed path (profiles/<client>/<name>/): claude files sessions by
project folder, so a path that changed with every upload would lose `--resume`. An upload
is unpacked beside it and swapped in by rename; the old copy is deleted once no run is
using it (a running claude keeps the copy it started in).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import NAME_RE

MAX_UPLOAD = 32 * 1024 * 1024
MAX_UNPACKED = 20 * 1024 * 1024
MAX_FILES = 2000
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}
DEFAULT = "_default"        # the empty profile of a run that names none (not a valid upload name)


class ProfileError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Profile:
    client: str
    name: str
    path: Path
    sha256: str
    files: int

    @property
    def system_prompt(self) -> Path | None:
        path = self.path / "SYSTEM.md"
        return path if path.is_file() else None

    def as_json(self) -> dict:
        return {"name": self.name, "sha256": self.sha256, "files": self.files}


def _allowed(path: PurePosixPath) -> bool:
    parts = path.parts
    if parts in (("SYSTEM.md",), ("CLAUDE.md",)):
        return True
    return (len(parts) >= 4 and parts[0] == ".claude" and parts[1] == "skills"
            and path.suffix.lower() in TEXT_SUFFIXES)


def _allowed_dir(path: PurePosixPath) -> bool:
    parts = path.parts
    return parts in ((), (".claude",)) or parts[:2] == (".claude", "skills")


def _member_path(name: str) -> PurePosixPath | None:
    if "\\" in name or "\x00" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute():
        return None
    parts = [p for p in path.parts if p != "."]
    if any(p == ".." for p in parts):
        return None
    return PurePosixPath(*parts) if parts else PurePosixPath()


def read_upload(data: bytes) -> dict[str, bytes]:
    """The files of an uploaded tarball (gzip or plain), checked: path → content."""
    if len(data) > MAX_UPLOAD:
        raise ProfileError([f"the upload is larger than {MAX_UPLOAD // (1024 * 1024)} MB"])
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ProfileError([f"not a tar archive: {e}"])
    files: dict[str, bytes] = {}
    errors: list[str] = []
    total = 0
    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, OSError, EOFError) as e:
            raise ProfileError([f"a damaged tar archive: {e}"])
        if len(members) > MAX_FILES * 2:
            raise ProfileError([f"more than {MAX_FILES} files"])
        for member in members:
            path = _member_path(member.name)
            if path is None:
                errors.append(f"{member.name}: not a plain relative path")
                continue
            if member.isdir():
                if not _allowed_dir(path):
                    errors.append(f"{member.name}/: not allowed in a profile")
                continue
            if not member.isreg():
                errors.append(f"{member.name}: only plain files (no links or devices)")
                continue
            if not _allowed(path):
                errors.append(f"{member.name}: not allowed (only SYSTEM.md, CLAUDE.md and text files "
                              f"in .claude/skills/<skill>/)")
                continue
            total += member.size
            if total > MAX_UNPACKED:
                raise ProfileError([f"more than {MAX_UNPACKED // (1024 * 1024)} MB unpacked"])
            handle = archive.extractfile(member)
            content = handle.read() if handle else b""
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{member.name}: not UTF-8 text")
                continue
            key = str(path)
            if key in files:
                errors.append(f"{member.name}: twice in the archive")
                continue
            files[key] = content
    if len(files) > MAX_FILES:
        errors.append(f"more than {MAX_FILES} files")
    if errors:
        raise ProfileError(errors)
    return files


def content_hash(files: dict[str, bytes]) -> str:
    """The same files give the same hash however they were packed."""
    h = hashlib.sha256()
    for name in sorted(files):
        content = files[name]
        h.update(name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    return h.hexdigest()


class ProfileStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._in_use: dict[tuple[str, str], int] = {}
        self._doomed: dict[tuple[str, str], list[Path]] = {}

    def _folder(self, client: str) -> Path:
        return self.root / client

    def _meta_path(self, client: str, name: str) -> Path:
        return self._folder(client) / f".{name}.json"

    def get(self, client: str, name: str) -> Profile | None:
        if name == DEFAULT:
            path = self._folder(client) / DEFAULT
            path.mkdir(parents=True, exist_ok=True)
            return Profile(client, DEFAULT, path, "", 0)
        if not NAME_RE.match(name or ""):
            return None
        path = self._folder(client) / name
        try:
            meta = json.loads(self._meta_path(client, name).read_text())
        except (OSError, ValueError):
            return None
        if not path.is_dir():
            return None
        return Profile(client, name, path, str(meta.get("sha256", "")), int(meta.get("files", 0)))

    def list(self, client: str) -> list[Profile]:
        folder = self._folder(client)
        if not folder.is_dir():
            return []
        out = []
        for meta in sorted(folder.glob(".*.json")):
            profile = self.get(client, meta.name[1:-len(".json")])
            if profile:
                out.append(profile)
        return out

    def put(self, client: str, name: str, data: bytes) -> Profile:
        if not NAME_RE.match(name or ""):
            raise ProfileError(["a profile's name must be lowercase letters, digits, - or _ "
                                "(starting with a letter or digit)"])
        files = read_upload(data)
        sha = content_hash(files)
        with self._lock:
            current = self.get(client, name)
            if current and current.sha256 == sha:
                return current
            folder = self._folder(client)
            folder.mkdir(parents=True, exist_ok=True)
            fresh = folder / f".{name}.new-{uuid.uuid4().hex[:8]}"
            try:
                for rel, content in files.items():
                    target = fresh / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    os.chmod(target, 0o644)
                fresh.mkdir(exist_ok=True)
                path = folder / name
                old = None
                if path.exists():
                    old = folder / f".{name}.old-{uuid.uuid4().hex[:8]}"
                    os.rename(path, old)
                os.rename(fresh, path)
            except OSError:
                shutil.rmtree(fresh, ignore_errors=True)
                raise
            meta = {"sha256": sha, "files": len(files)}
            tmp = self._meta_path(client, name).with_suffix(".tmp")
            tmp.write_text(json.dumps(meta))
            os.replace(tmp, self._meta_path(client, name))
            if old:
                self._retire(client, name, old)
            return Profile(client, name, path, sha, len(files))

    def delete(self, client: str, name: str) -> bool:
        with self._lock:
            profile = self.get(client, name)
            if not profile or name == DEFAULT:
                return False
            self._meta_path(client, name).unlink(missing_ok=True)
            old = profile.path.with_name(f".{name}.old-{uuid.uuid4().hex[:8]}")
            os.rename(profile.path, old)
            self._retire(client, name, old)
            return True

    def _retire(self, client: str, name: str, old: Path) -> None:
        """Delete an old copy now, or when the last run in it ends. (Lock held.)"""
        if self._in_use.get((client, name)):
            self._doomed.setdefault((client, name), []).append(old)
        else:
            shutil.rmtree(old, ignore_errors=True)

    @contextmanager
    def using(self, client: str, name: str):
        key = (client, name)
        with self._lock:
            self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._in_use[key] -= 1
                if not self._in_use[key]:
                    del self._in_use[key]
                    for old in self._doomed.pop(key, []):
                        shutil.rmtree(old, ignore_errors=True)

    def sweep(self) -> None:
        """At start-up: copies left by a broker that stopped mid-upload or mid-run."""
        if not self.root.is_dir():
            return
        for folder in self.root.iterdir():
            if folder.is_dir():
                for leftover in folder.glob(".*.*-*"):
                    if leftover.is_dir() and (".new-" in leftover.name or ".old-" in leftover.name):
                        shutil.rmtree(leftover, ignore_errors=True)
