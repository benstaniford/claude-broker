"""The broker's configuration: `broker.toml` and one `clients.d/<name>.toml` per client app.

    ~/.config/claude-broker/broker.toml
    ~/.config/claude-broker/clients.d/<client>.toml
    ~/.local/state/claude-broker/      profiles, sessions
    $XDG_RUNTIME_DIR/claude-broker/<client>/broker.sock   (and the client's tools.sock)

CLAUDE_BROKER_CONFIG, CLAUDE_BROKER_STATE and CLAUDE_BROKER_RUNTIME move these (tests do).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .claude import BUILTIN_TOOLS, EFFORTS

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")   # client and profile names
BROKER_SOCKET = "broker.sock"
TOOLS_SOCKET = "tools.sock"


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_BROKER_CONFIG") or Path.home() / ".config" / "claude-broker")


def state_dir() -> Path:
    return Path(os.environ.get("CLAUDE_BROKER_STATE") or Path.home() / ".local" / "state" / "claude-broker")


def runtime_dir() -> Path:
    if os.environ.get("CLAUDE_BROKER_RUNTIME"):
        return Path(os.environ["CLAUDE_BROKER_RUNTIME"])
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "claude-broker"


def find_claude() -> str | None:
    """claude on PATH, or where its installer puts it (~/.local/bin is not on a service's PATH)."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if os.access(local, os.X_OK) else None


def is_socket(path: Path) -> bool:
    """A unix socket itself, not a symlink to one: a client's folder is writable from its
    container, which could otherwise point tools.sock at any socket on the host."""
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


@dataclass
class BrokerConfig:
    claude: str = ""                 # empty: find_claude() at each run
    max_concurrent: int = 2          # claude runs at once, all clients together (one plan's limits)
    keep_finished_seconds: int = 1800
    default_model: str = ""
    log_level: str = "info"

    def claude_bin(self) -> str | None:
        return self.claude or find_claude()


@dataclass
class ClientConfig:
    name: str
    workspace_roots: list[Path] = field(default_factory=list)   # folders runs may read and write
    read_only_roots: list[Path] = field(default_factory=list)   # folders runs may only read
    models: list[str] = field(default_factory=list)             # empty: any model
    default_model: str = ""
    default_effort: str = ""
    max_concurrent: int = 1
    max_queued: int = 20                                        # runs waiting for a turn
    max_run_seconds: int = 900
    allowed_builtin_tools: list[str] = field(default_factory=lambda: list(BUILTIN_TOOLS))

    def socket_dir(self) -> Path:
        return runtime_dir() / self.name

    def broker_socket(self) -> Path:
        return self.socket_dir() / BROKER_SOCKET

    def tools_socket(self) -> Path:
        return self.socket_dir() / TOOLS_SOCKET

    def has_tools(self) -> bool:
        return is_socket(self.tools_socket())

    def root_for(self, path: Path) -> tuple[Path, bool] | None:
        """The root a (resolved) folder sits under, and whether it may be written; the most
        specific root wins, so a read-only folder inside a workspace stays read-only."""
        best: tuple[Path, bool] | None = None
        for roots, writable in ((self.workspace_roots, True), (self.read_only_roots, False)):
            for root in roots:
                if path == root or root in path.parents:
                    if best is None or len(root.parts) >= len(best[0].parts):
                        best = (root, writable)
        return best


def _read(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}")
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}")


def _int(data: dict, key: str, default: int, low: int, high: int, where: Path) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"{where}: {key} must be a whole number from {low} to {high}")
    return value


def _str(data: dict, key: str, default: str, where: Path) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string")
    return value


def _strings(data: dict, key: str, default: list[str], where: Path) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return list(value)


def _roots(data: dict, key: str, where: Path) -> list[Path]:
    out = []
    for raw in _strings(data, key, [], where):
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            raise ConfigError(f"{where}: {key}: {raw} is not an absolute path")
        out.append(path.resolve())
    return out


def load_broker(path: Path | None = None) -> BrokerConfig:
    path = path or config_dir() / "broker.toml"
    if not path.exists():
        return BrokerConfig()
    data = _read(path)
    unknown = set(data) - {"claude", "max_concurrent", "keep_finished_seconds", "default_model", "log_level"}
    if unknown:
        raise ConfigError(f"{path}: unknown settings: {', '.join(sorted(unknown))}")
    level = _str(data, "log_level", "info", path).lower()
    if level not in ("debug", "info", "warning", "error"):
        raise ConfigError(f"{path}: log_level must be debug, info, warning or error")
    return BrokerConfig(
        claude=os.path.expanduser(_str(data, "claude", "", path)),
        max_concurrent=_int(data, "max_concurrent", 2, 1, 32, path),
        keep_finished_seconds=_int(data, "keep_finished_seconds", 1800, 0, 7 * 86400, path),
        default_model=_str(data, "default_model", "", path),
        log_level=level,
    )


def load_client(path: Path) -> ClientConfig:
    name = path.stem
    if not NAME_RE.match(name):
        raise ConfigError(f"{path}: a client's name (the file's) must be lowercase letters, digits, - or _")
    data = _read(path)
    known = {"workspace_roots", "read_only_roots", "models", "default_model", "default_effort",
             "max_concurrent", "max_queued", "max_run_seconds", "allowed_builtin_tools"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown settings: {', '.join(sorted(unknown))}")
    tools = _strings(data, "allowed_builtin_tools", list(BUILTIN_TOOLS), path)
    bad = [t for t in tools if t not in BUILTIN_TOOLS]
    if bad:
        raise ConfigError(f"{path}: allowed_builtin_tools: {', '.join(bad)} not offered "
                          f"(only {', '.join(BUILTIN_TOOLS)})")
    effort = _str(data, "default_effort", "", path)
    if effort and effort not in EFFORTS:
        raise ConfigError(f"{path}: default_effort must be one of {', '.join(EFFORTS)}")
    client = ClientConfig(
        name=name,
        workspace_roots=_roots(data, "workspace_roots", path),
        read_only_roots=_roots(data, "read_only_roots", path),
        models=_strings(data, "models", [], path),
        default_model=_str(data, "default_model", "", path),
        default_effort=effort,
        max_concurrent=_int(data, "max_concurrent", 1, 1, 32, path),
        max_queued=_int(data, "max_queued", 20, 0, 1000, path),
        max_run_seconds=_int(data, "max_run_seconds", 900, 10, 6 * 3600, path),
        allowed_builtin_tools=tools,
    )
    if client.models and client.default_model and client.default_model not in client.models:
        raise ConfigError(f"{path}: default_model {client.default_model} isn't in models")
    return client


def load_clients(folder: Path | None = None) -> tuple[dict[str, ClientConfig], list[str]]:
    """Every client, and the problems with files that couldn't be read: one bad file doesn't
    take the other clients down."""
    folder = folder or config_dir() / "clients.d"
    clients, problems = {}, []
    if folder.is_dir():
        for path in sorted(folder.glob("*.toml")):
            try:
                client = load_client(path)
            except ConfigError as e:
                problems.append(str(e))
                continue
            clients[client.name] = client
    return clients, problems


# --- writing ------------------------------------------------------------------------------

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (str, Path)):
        return json.dumps(str(value))       # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"can't write {value!r} as TOML")


def dump_toml(values: dict) -> str:
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in values.items())


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def client_file(name: str) -> Path:
    return config_dir() / "clients.d" / f"{name}.toml"


def write_client(client: ClientConfig) -> Path:
    values = {
        "workspace_roots": [str(p) for p in client.workspace_roots],
        "read_only_roots": [str(p) for p in client.read_only_roots],
        "models": client.models,
        "default_model": client.default_model,
        "default_effort": client.default_effort,
        "max_concurrent": client.max_concurrent,
        "max_queued": client.max_queued,
        "max_run_seconds": client.max_run_seconds,
        "allowed_builtin_tools": client.allowed_builtin_tools,
    }
    path = client_file(client.name)
    write_atomic(path, f"# claude-broker client: {client.name}\n" + dump_toml(values))
    load_client(path)     # what was written must read back
    return path
