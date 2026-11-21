"""The `claude-broker` command."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import VERSION
from .claude import BUILTIN_TOOLS, EFFORTS
from .config import (NAME_RE, ClientConfig, ConfigError, client_file, config_dir, dump_toml, load_broker,
                     load_client, load_clients, runtime_dir, state_dir, write_atomic, write_client, find_claude)

SERVICE = "claude-broker.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _reload_service() -> None:
    """Tell a running broker to read its config again."""
    if not shutil.which("systemctl"):
        print("Reload the broker (SIGHUP) to pick this up.")
        return
    if _systemctl("is-active", "--quiet", SERVICE).returncode == 0:
        result = _systemctl("reload", SERVICE)
        print("Broker reloaded." if result.returncode == 0 else
              f"Couldn't reload the broker: {result.stderr.strip()}")
    else:
        print("The broker isn't running (systemctl --user start claude-broker).")


# --- serve and mcp-proxy ---------------------------------------------------------------------

def cmd_serve(args) -> int:
    from .server import Broker
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        broker = Broker()
    except ConfigError as e:
        print(f"claude-broker: {e}", file=sys.stderr)
        return 2
    broker.serve()
    return 0


def cmd_mcp_proxy(args) -> int:
    from .mcp_proxy import relay
    return relay(args.socket)


# --- clients ---------------------------------------------------------------------------------

def _abs(paths: list[str] | None) -> list[Path]:
    return [Path(os.path.expanduser(p)).resolve() for p in paths or []]


def cmd_client_add(args) -> int:
    if not NAME_RE.match(args.name):
        print("A client's name is lowercase letters, digits, - or _ (up to 40).", file=sys.stderr)
        return 2
    path = client_file(args.name)
    if path.exists() and not args.force:
        print(f"{path} exists; --force to replace it.", file=sys.stderr)
        return 1
    tools = [t.strip() for t in args.tools.split(",")] if args.tools else list(BUILTIN_TOOLS)
    client = ClientConfig(
        name=args.name, workspace_roots=_abs(args.root), read_only_roots=_abs(args.read_only),
        models=args.model or [], default_model=args.default_model or "", default_effort=args.effort or "",
        max_concurrent=args.max_concurrent, max_queued=args.max_queued, max_run_seconds=args.max_run_seconds, allowed_builtin_tools=tools)
    for root in client.workspace_roots + client.read_only_roots:
        if not root.is_dir():
            print(f"warning: {root} isn't a folder (yet)", file=sys.stderr)
    try:
        written = write_client(client)
    except ConfigError as e:
        print(f"claude-broker: {e}", file=sys.stderr)
        path.unlink(missing_ok=True)
        return 2
    print(f"Wrote {written}")
    print(f"Its socket: {client.broker_socket()}")
    print(f"Mount the folder in its container, e.g. (docker compose):\n"
          f"    volumes:\n      - {client.socket_dir()}:/run/claude-broker")
    _reload_service()
    return 0


def cmd_client_list(args) -> int:
    clients, problems = load_clients()
    for client in clients.values():
        print(f"{client.name}")
        print(f"  socket:      {client.broker_socket()}" + ("" if client.broker_socket().exists() else "  (not listening)"))
        print(f"  tools.sock:  {'present' if client.has_tools() else 'absent'}")
        print(f"  read/write:  {', '.join(map(str, client.workspace_roots)) or '-'}")
        print(f"  read only:   {', '.join(map(str, client.read_only_roots)) or '-'}")
        print(f"  models:      {', '.join(client.models) or 'any'}"
              + (f" (default {client.default_model})" if client.default_model else ""))
        print(f"  limits:      {client.max_concurrent} at once, {client.max_queued} waiting, "
              f"{client.max_run_seconds}s a run")
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if not clients and not problems:
        print("No clients yet: claude-broker client add <name> --root <folder>")
    return 1 if problems else 0


def cmd_client_remove(args) -> int:
    path = client_file(args.name)
    if not path.exists():
        print(f"No client called {args.name}.", file=sys.stderr)
        return 1
    path.unlink()
    print(f"Removed {path}")
    _reload_service()
    return 0


# --- setup and check ------------------------------------------------------------------------

def cmd_setup(args) -> int:
    broker_toml = config_dir() / "broker.toml"
    claude = find_claude()
    if not broker_toml.exists():
        write_atomic(broker_toml, "# claude-broker: see `claude-broker --help` and the README\n" + dump_toml({
            "claude": claude or "",
            "max_concurrent": 2,
            "keep_finished_seconds": 1800,
            "default_model": "",
            "log_level": "info",
        }))
        print(f"Wrote {broker_toml}")
    else:
        print(f"Kept {broker_toml}")
    (config_dir() / "clients.d").mkdir(parents=True, exist_ok=True)
    user = os.environ.get("USER") or ""
    if not args.no_linger and shutil.which("loginctl") and user:
        result = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
        print("Linger on: the broker runs without a login session." if result.returncode == 0 else
              f"Couldn't turn linger on ({result.stderr.strip()}); try: sudo loginctl enable-linger {user}")
    if not args.no_enable and shutil.which("systemctl"):
        _systemctl("daemon-reload")
        result = _systemctl("enable", "--now", SERVICE)
        print("Service enabled and started." if result.returncode == 0 else
              f"Couldn't start the service: {result.stderr.strip()}")
    print()
    return cmd_check(args)


def _claude_login(claude: str) -> tuple[bool, str]:
    try:
        result = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30)
        status = json.loads(result.stdout)
        if status.get("loggedIn"):
            return True, f"logged in ({status.get('authMethod', '?')})"
        return False, "not logged in: run `claude` once and sign in"
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return False, f"couldn't ask claude: {e}"


def cmd_check(args) -> int:
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else '!!'}] {text}")

    try:
        broker = load_broker()
        line(True, f"config {config_dir() / 'broker.toml'}")
    except ConfigError as e:
        line(False, str(e))
        return 1
    claude = broker.claude_bin()
    if claude:
        line(True, f"claude: {claude}")
        line(*_claude_login(claude))
    else:
        line(False, "claude not found: install Claude Code for this user, or set `claude` in broker.toml")
    clients, problems = load_clients()
    for problem in problems:
        line(False, problem)
    for client in clients.values():
        listening = client.broker_socket().exists()
        line(listening, f"client {client.name}: {client.broker_socket()}" + ("" if listening else " not listening"))
        for root in client.workspace_roots + client.read_only_roots:
            if not root.is_dir():
                line(False, f"client {client.name}: root {root} isn't a folder")
    if not clients:
        print("[--] no clients yet: claude-broker client add <name> --root <folder>")
    runtime = runtime_dir()
    line(runtime.parent.is_dir(), f"runtime folder {runtime}")
    user = os.environ.get("USER") or ""
    if shutil.which("loginctl") and user:
        result = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"],
                                capture_output=True, text=True)
        linger = result.stdout.strip() == "yes"
        line(linger, "linger on" if linger else f"linger off: the broker stops when you log out "
                                                f"(loginctl enable-linger {user})")
    if shutil.which("systemctl"):
        active = _systemctl("is-active", "--quiet", SERVICE).returncode == 0
        line(active, "service running" if active else "service not running: systemctl --user start claude-broker")
    print(f"state: {state_dir()}")
    return 0 if ok else 1


# --- talking to a running broker ---------------------------------------------------------------

def _socket(args) -> str:
    if args.socket:
        return args.socket
    if not args.client:
        raise SystemExit("--client NAME or --socket PATH")
    return str(runtime_dir() / args.client / "broker.sock")


def _json_arg(value: str | None):
    if value is None:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text()
    return json.loads(value)


def cmd_call(args) -> int:
    from .client import BrokerClient, BrokerError
    client = BrokerClient(_socket(args), timeout=None)
    add_dirs = []
    for spec in args.add_dir or []:
        path, _, mode = spec.partition(":")
        add_dirs.append({"path": str(Path(os.path.expanduser(path)).resolve()), "write": mode == "rw"})
    try:
        events = client.run(
            args.prompt, run_id=args.run_id, profile=args.profile,
            session={"resume": args.resume} if args.resume else None, schema=_json_arg(args.schema),
            model=args.model, effort=args.effort, add_dirs=add_dirs or None,
            builtin_tools=args.tools.split(",") if args.tools else None,
            mcp_tools=args.mcp_tool, stream_fields=args.field, timeout=args.timeout)
        code = 1
        for event in events:
            if args.raw:
                print(json.dumps(event), flush=True)
            elif event["type"] == "field":
                sys.stderr.write(event.get("delta") or "\n" + event.get("text", ""))
                sys.stderr.flush()
            elif event["type"] == "field_closed":
                sys.stderr.write("\n")
            elif event["type"] in ("started", "queued", "tool"):
                print(json.dumps(event), file=sys.stderr, flush=True)
            if event["type"] == "result":
                code = 0
                if not args.raw:
                    print(json.dumps(event, indent=2))
            elif event["type"] == "error":
                if not args.raw:
                    print(f"error: {event.get('kind')}: {event.get('detail')}", file=sys.stderr)
        return code
    except BrokerError as e:
        print(f"claude-broker: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"claude-broker: can't reach {client.socket_path}: {e}", file=sys.stderr)
        return 1


def cmd_profile_put(args) -> int:
    from .client import BrokerClient, BrokerError
    try:
        print(json.dumps(BrokerClient(_socket(args)).put_profile(args.name, args.folder)))
        return 0
    except BrokerError as e:
        print(f"claude-broker: {e}", file=sys.stderr)
        return 1


def cmd_profile_list(args) -> int:
    from .client import BrokerClient
    for profile in BrokerClient(_socket(args)).profiles():
        print(f"{profile['name']}  {profile['files']} files  {profile['sha256'][:12]}")
    return 0


def cmd_version(args) -> int:
    print(VERSION)
    return 0


# --- argument parsing -------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-broker",
                                description="Claude Code as a local service for containers, over unix sockets.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    sub.add_parser("serve", help="run the broker (systemd does this)").set_defaults(func=cmd_serve)

    s = sub.add_parser("setup", help="write a default config, turn linger on, enable the user service")
    s.add_argument("--no-linger", action="store_true", help="leave linger as it is")
    s.add_argument("--no-enable", action="store_true", help="don't enable or start the service")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("check", help="check claude, the config, clients and the service").set_defaults(func=cmd_check)

    s = sub.add_parser("mcp-proxy", help="(started by claude) relay stdio MCP to a client's tools.sock")
    s.add_argument("--socket", required=True)
    s.set_defaults(func=cmd_mcp_proxy)

    client = sub.add_parser("client", help="add, list or remove client apps").add_subparsers(dest="action",
                                                                                              required=True)
    s = client.add_parser("add", help="add a client: its own socket, and the folders its runs may use")
    s.add_argument("name")
    s.add_argument("--root", action="append", metavar="FOLDER", help="a folder runs may read and write (repeat)")
    s.add_argument("--read-only", action="append", metavar="FOLDER", help="a folder runs may only read (repeat)")
    s.add_argument("--model", action="append", metavar="MODEL", help="a model it may use (repeat; none: any)")
    s.add_argument("--default-model", metavar="MODEL")
    s.add_argument("--effort", choices=EFFORTS, help="default effort")
    s.add_argument("--max-concurrent", type=int, default=1)
    s.add_argument("--max-queued", type=int, default=20, help="runs that may wait for a turn")
    s.add_argument("--max-run-seconds", type=int, default=900)
    s.add_argument("--tools", help=f"built-in tools it may ask for (default: {','.join(BUILTIN_TOOLS)})")
    s.add_argument("--force", action="store_true", help="replace an existing client")
    s.set_defaults(func=cmd_client_add)
    client.add_parser("list", help="list clients").set_defaults(func=cmd_client_list)
    s = client.add_parser("remove", help="remove a client")
    s.add_argument("name")
    s.set_defaults(func=cmd_client_remove)

    def target(s):
        s.add_argument("--client", help="the client whose socket to use")
        s.add_argument("--socket", help="a socket path instead")

    s = sub.add_parser("call", help="start a run through a client's socket and show its events")
    target(s)
    s.add_argument("prompt")
    s.add_argument("--profile")
    s.add_argument("--schema", help="a JSON schema, or @file")
    s.add_argument("--resume", metavar="SESSION_ID")
    s.add_argument("--run-id")
    s.add_argument("--model")
    s.add_argument("--effort", choices=EFFORTS)
    s.add_argument("--add-dir", action="append", metavar="FOLDER[:rw]")
    s.add_argument("--tools", help="built-in tools, comma separated")
    s.add_argument("--mcp-tool", action="append", metavar="NAME")
    s.add_argument("--field", action="append", metavar="NAME", help="stream this top-level string field")
    s.add_argument("--timeout", type=float)
    s.add_argument("--raw", action="store_true", help="print every event as NDJSON")
    s.set_defaults(func=cmd_call)

    profile = sub.add_parser("profile", help="upload or list a client's profiles").add_subparsers(
        dest="action", required=True)
    s = profile.add_parser("put", help="upload a folder as a profile")
    target(s)
    s.add_argument("name")
    s.add_argument("folder")
    s.set_defaults(func=cmd_profile_put)
    s = profile.add_parser("list", help="list profiles")
    target(s)
    s.set_defaults(func=cmd_profile_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)
