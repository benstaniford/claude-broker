"""The broker: one HTTP/1.1 listener per client, each on its own unix socket.

The socket a request comes in on says which client sent it; there is no other
authentication. A client's socket sits alone in its own folder, which is what a container
mounts (mounting the socket file itself would go stale when the broker restarts).

    GET    /v1/health
    GET    /v1/usage
    GET    /v1/runs                     this client's runs
    POST   /v1/runs                     start (or rejoin) a run: NDJSON stream of its events
    GET    /v1/runs/<id>?from=<seq>     rejoin a run: NDJSON stream
    DELETE /v1/runs/<id>                cancel it
    GET    /v1/profiles
    PUT    /v1/profiles/<name>          body: a tar or tar.gz of the profile
    DELETE /v1/profiles/<name>
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import VERSION
from .api import validate_run
from .claude import RunFailed
from .config import (BrokerConfig, ClientConfig, ConfigError, config_dir, load_broker, load_clients,
                     runtime_dir, state_dir)
from .profiles import MAX_UPLOAD, ProfileError, ProfileStore
from .runs import Registry
from .sessions import SessionStore

log = logging.getLogger("claude_broker")

MAX_JSON_BODY = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"claude-broker/{VERSION}"
    timeout = 600            # an idle keep-alive connection is dropped after this

    server: "ClientServer"

    # --- plumbing -----------------------------------------------------------------------

    def address_string(self) -> str:     # a unix socket has no peer address
        return self.server.client_name

    def log_message(self, format: str, *args) -> None:
        log.debug("%s: " + format, self.server.client_name, *args)

    @property
    def broker(self) -> "Broker":
        return self.server.broker

    def _send_json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, *errors: str) -> None:
        self._send_json(status, {"errors": list(errors)})

    def _body(self, limit: int) -> bytes | None:
        """The request body, or None once an error has been sent."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._error(411, "send the body with a Content-Length")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self.close_connection = True
            self._error(413, f"the body must be at most {limit} bytes")
            return None
        return self.rfile.read(length) if length else b""

    def _stream(self, run, start: int, created: bool | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Run-Id", run.id)
        if created is not None:
            self.send_header("X-Run-Created", "true" if created else "false")
        self.end_headers()
        try:
            for event in run.follow(start):
                line = json.dumps(event).encode() + b"\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            log.debug("%s: stopped following run %s (client went away)", self.server.client_name, run.id)
            self.close_connection = True

    def _client(self) -> ClientConfig | None:
        client = self.broker.clients.get(self.server.client_name)
        if not client:
            self.close_connection = True
            self._error(503, "this client has been removed from the broker")
        return client

    def _route(self, method: str) -> None:
        try:
            client = self._client()
            if not client:
                return
            url = urlsplit(self.path)
            parts = [unquote(p) for p in url.path.split("/") if p]
            query = parse_qs(url.query)
            if parts[:1] != ["v1"]:
                return self._error(404, "no such endpoint")
            handler = getattr(self, f"_{method}_{'_'.join(parts[1:2]) or 'root'}", None)
            if not handler:
                return self._error(404 if method == "get" else 405, "no such endpoint")
            handler(client, parts[2:], query)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            log.exception("%s: %s %s failed", self.server.client_name, method.upper(), self.path)
            self.close_connection = True
            try:
                self._error(500, "the broker failed; see its log")
            except OSError:
                pass

    def do_GET(self):
        self._route("get")

    def do_POST(self):
        self._route("post")

    def do_PUT(self):
        self._route("put")

    def do_DELETE(self):
        self._route("delete")

    # --- endpoints ---------------------------------------------------------------------------

    def _get_health(self, client: ClientConfig, rest, query) -> None:
        broker = self.broker
        self._send_json(200, {
            "ok": True, "version": VERSION, "client": client.name,
            "claude": broker.config.claude_bin(),
            "tools_socket": client.has_tools(),
            "models": client.models, "max_run_seconds": client.max_run_seconds,
            **broker.registry.stats(client.name),
        })

    def _get_usage(self, client, rest, query) -> None:
        self._send_json(200, {"rate_limits": self.broker.registry.rate_limits})

    def _get_runs(self, client: ClientConfig, rest, query) -> None:
        if not rest:
            return self._send_json(200, {"runs": self.broker.registry.list(client.name)})
        run = self.broker.registry.get(client.name, rest[0])
        if not run or len(rest) > 1:
            return self._error(404, "no such run (finished runs are kept for a while, then forgotten)")
        try:
            start = int((query.get("from") or ["0"])[0])
        except ValueError:
            return self._error(400, "from: an event number")
        self._stream(run, start, None)

    def _post_runs(self, client: ClientConfig, rest, query) -> None:
        if rest:
            return self._error(405, "POST /v1/runs")
        raw = self._body(MAX_JSON_BODY)
        if raw is None:
            return
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            return self._error(400, "the body isn't JSON")
        broker = self.broker
        request, errors = validate_run(body, client, broker.config, broker.profiles, broker.sessions)
        if errors or request is None:
            return self._error(400, *errors)
        try:
            run, created = broker.registry.submit(client, request)
        except RunFailed as e:
            return self._error(503, e.detail)
        self._stream(run, 0, created)

    def _delete_runs(self, client: ClientConfig, rest, query) -> None:
        if len(rest) != 1:
            return self._error(405, "DELETE /v1/runs/<id>")
        run = self.broker.registry.cancel(client.name, rest[0])
        if not run:
            return self._error(404, "no such run")
        self._send_json(200, run.summary())

    def _get_profiles(self, client: ClientConfig, rest, query) -> None:
        profiles = self.broker.profiles
        if not rest:
            return self._send_json(200, {"profiles": [p.as_json() for p in profiles.list(client.name)]})
        profile = profiles.get(client.name, rest[0]) if len(rest) == 1 else None
        if not profile or rest[0].startswith("_"):
            return self._error(404, "no such profile")
        self._send_json(200, profile.as_json())

    def _put_profiles(self, client: ClientConfig, rest, query) -> None:
        if len(rest) != 1:
            return self._error(405, "PUT /v1/profiles/<name>")
        data = self._body(MAX_UPLOAD)
        if data is None:
            return
        try:
            profile = self.broker.profiles.put(client.name, rest[0], data)
        except ProfileError as e:
            return self._error(400, *e.errors)
        self._send_json(200, profile.as_json())

    def _delete_profiles(self, client: ClientConfig, rest, query) -> None:
        if len(rest) != 1 or not self.broker.profiles.delete(client.name, rest[0]):
            return self._error(404, "no such profile")
        self._send_json(200, {"deleted": rest[0]})


class ClientServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, broker: "Broker", client_name: str, path: Path):
        self.broker = broker
        self.client_name = client_name
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise OSError(f"{path} exists and isn't a socket")
            path.unlink()                      # a socket left by a broker that stopped
        super().__init__(str(path), Handler, bind_and_activate=False)
        old = os.umask(0o177)
        try:
            self.server_bind()
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.inode = path.stat().st_ino
        self.server_activate()

    def remove_socket(self) -> None:
        try:
            if self.path.stat().st_ino == self.inode:
                self.path.unlink()
        except OSError:
            pass


class Broker:
    def __init__(self, broker_config: Path | None = None, clients_dir: Path | None = None):
        self.broker_config_path = broker_config or config_dir() / "broker.toml"
        self.clients_dir = clients_dir or config_dir() / "clients.d"
        self.config: BrokerConfig = load_broker(self.broker_config_path)
        self.clients: dict[str, ClientConfig] = {}
        state = state_dir()
        self.profiles = ProfileStore(state / "profiles")
        self.sessions = SessionStore(state / "sessions.json")
        self.registry = Registry(lambda: self.config, self.profiles, self.sessions, runtime_dir() / ".runs",
                                 self_command())
        self.servers: dict[str, tuple[ClientServer, threading.Thread]] = {}
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        self.profiles.sweep()
        self.reload()

    def reload(self) -> list[str]:
        """Read the config again: start listeners for new clients, stop removed ones. A bad
        broker.toml keeps the old settings."""
        problems = []
        with self._lock:
            try:
                self.config = load_broker(self.broker_config_path)
            except ConfigError as e:
                problems.append(str(e))
            logging.getLogger("claude_broker").setLevel(self.config.log_level.upper())
            clients, bad = load_clients(self.clients_dir)
            problems += bad
            self.clients = clients
            for name in list(self.servers):
                if name not in clients:
                    self._stop_server(name)
                    log.info("client %s removed", name)
            for name, client in clients.items():
                if name not in self.servers:
                    try:
                        server = ClientServer(self, name, client.broker_socket())
                    except OSError as e:
                        problems.append(f"{name}: can't listen at {client.broker_socket()}: {e}")
                        continue
                    thread = threading.Thread(target=server.serve_forever, name=f"listen-{name}", daemon=True)
                    thread.start()
                    self.servers[name] = (server, thread)
                    log.info("client %s listening at %s", name, client.broker_socket())
        for problem in problems:
            log.warning("config: %s", problem)
        return problems

    def _stop_server(self, name: str) -> None:
        server, thread = self.servers.pop(name)
        server.shutdown()
        server.server_close()
        server.remove_socket()
        thread.join(5)

    def stop(self) -> None:
        self._stop.set()

    def request_reload(self) -> None:
        self._reload.set()

    def serve(self) -> None:
        """Run until SIGTERM or SIGINT; SIGHUP reloads the config."""
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        signal.signal(signal.SIGHUP, lambda *_: self.request_reload())
        self.start()
        log.info("claude-broker %s: %d client(s); claude: %s", VERSION, len(self.servers),
                 self.config.claude_bin() or "NOT FOUND")
        last_reap = time.monotonic()
        while not self._stop.is_set():
            self._stop.wait(1)
            if self._reload.is_set():
                self._reload.clear()
                log.info("reloading the config")
                self.reload()
            if time.monotonic() - last_reap > 60:
                self.registry.reap()
                last_reap = time.monotonic()
        self.close()

    def close(self) -> None:
        log.info("stopping")
        with self._lock:
            for name in list(self.servers):
                self._stop_server(name)
        self.registry.shutdown()


def self_command() -> tuple[list[str], dict]:
    """How to start this program again (for `mcp-proxy`): the zipapp itself, or `python -m
    claude_broker` from a source checkout."""
    import sys
    import claude_broker
    package = Path(claude_broker.__file__).resolve().parent
    archive = package.parent
    if archive.is_file():                                  # inside a zipapp
        return [sys.executable, str(archive)], {}
    return [sys.executable, "-m", "claude_broker"], {"PYTHONPATH": str(archive)}


def connect(path: Path, timeout: float | None = None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    return sock
