"""Runs: one claude process each, its events kept so a client can reattach.

- A run is known by its client and `run_id`. Submitting an id that is running or recently
  finished gives the same run, so an app that restarts and resubmits never runs it twice.
- Every event is kept, numbered (`seq`), until the run has been finished for
  `keep_finished_seconds`; a follower gets them all from any point, then the live ones.
- A client going away doesn't stop its run: only `cancel` (or the timeout) does.
- At most `max_concurrent` runs at once over all clients, and each client's own cap; the
  rest wait in one queue, first come first served among clients with room.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .claude import ClaudeProcess, RunFailed, RunSpec, Cancelled
from .config import BrokerConfig, ClientConfig
from .fields import FieldStream
from .profiles import ProfileStore
from .sessions import SessionStore

log = logging.getLogger("claude_broker.runs")

PING_SECONDS = 15


@dataclass
class RunRequest:
    """A checked `POST /v1/runs` body (see api.validate_run)."""
    run_id: str
    prompt: str
    profile: str
    session_id: str
    resume: bool
    schema: dict | None = None
    model: str = ""
    effort: str = ""
    add_dirs: list[tuple[Path, bool]] = field(default_factory=list)
    builtin_tools: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    stream_fields: list[str] = field(default_factory=list)
    timeout: float = 900


class Run:
    def __init__(self, client: ClientConfig, request: RunRequest, clock: Callable[[], float]):
        self.client = client
        self.request = request
        self.id = request.run_id
        self.clock = clock
        self.created = clock()
        self.status = "queued"          # queued → running → done
        self.outcome = ""               # result | error
        self.finished_at: float | None = None
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.process: ClaudeProcess | None = None
        self.cancel_requested = False

    def add(self, event: dict) -> None:
        with self.cond:
            event = {"seq": len(self.events), **event}
            self.events.append(event)
            if event["type"] in ("result", "error"):
                self.outcome = event["type"]
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.status = "done"
            self.finished_at = self.clock()
            self.cond.notify_all()

    def follow(self, start: int = 0, ping: float = PING_SECONDS) -> Iterator[dict]:
        """Every event from `start` on, then the live ones until the run ends; a `ping` after
        each `ping` seconds of quiet, so a client can tell a slow run from a dead socket."""
        i = max(0, start)
        while True:
            with self.cond:
                if i >= len(self.events) and self.status != "done":
                    self.cond.wait(ping)
                batch = self.events[i:]
                done = self.status == "done"
            if batch:
                i += len(batch)
                yield from batch
            elif done:
                return
            else:
                yield {"type": "ping"}
            if done and i >= len(self.events):
                return

    def summary(self) -> dict:
        with self.cond:
            return {"run_id": self.id, "status": self.status, "outcome": self.outcome or None,
                    "events": len(self.events), "created": int(self.created),
                    "finished": int(self.finished_at) if self.finished_at else None}


class Registry:
    def __init__(self, broker: Callable[[], BrokerConfig], profiles: ProfileStore, sessions: SessionStore,
                 work_dir: Path, self_command: tuple[list[str], dict], clock: Callable[[], float] = time.time):
        self.broker = broker
        self.profiles = profiles
        self.sessions = sessions
        self.work_dir = work_dir                 # per-run MCP configs
        self.self_command = self_command         # how to start `claude-broker mcp-proxy`: (argv, env)
        self.clock = clock
        self.cond = threading.Condition()
        self.runs: dict[tuple[str, str], Run] = {}
        self.queue: list[Run] = []
        self.running: dict[str, int] = {}
        self.threads: set[threading.Thread] = set()
        self.rate_limits: dict | None = None
        self.closed = False

    # --- asking -----------------------------------------------------------------------

    def get(self, client: str, run_id: str) -> Run | None:
        with self.cond:
            return self.runs.get((client, run_id))

    def stats(self, client: str | None = None) -> dict:
        with self.cond:
            runs = [r for r in self.runs.values() if client is None or r.client.name == client]
            return {"running": sum(r.status == "running" for r in runs),
                    "queued": sum(r.status == "queued" for r in runs)}

    def list(self, client: str) -> list[dict]:
        with self.cond:
            runs = [r for r in self.runs.values() if r.client.name == client]
        return [r.summary() for r in sorted(runs, key=lambda r: r.created)]

    # --- submitting and cancelling ------------------------------------------------------

    def submit(self, client: ClientConfig, request: RunRequest) -> tuple[Run, bool]:
        """The run for this id: an existing one (False), or a new one, queued (True)."""
        self.reap()
        with self.cond:
            if self.closed:
                raise RunFailed("the broker is stopping", kind="stopping")
            existing = self.runs.get((client.name, request.run_id))
            if existing:
                return existing, False
            run = Run(client, request, self.clock)
            self.runs[(client.name, run.id)] = run
            self.queue.append(run)
            thread = threading.Thread(target=self._thread, args=(run,), name=f"run-{client.name}-{run.id}",
                                      daemon=True)
            self.threads.add(thread)
        thread.start()
        return run, True

    def cancel(self, client: str, run_id: str) -> Run | None:
        with self.cond:
            run = self.runs.get((client, run_id))
            if not run:
                return None
            run.cancel_requested = True
            process = run.process
            self.cond.notify_all()
        if process:
            process.kill(Cancelled("cancelled by the client"))
        return run

    def shutdown(self, wait: float = 10) -> None:
        with self.cond:
            self.closed = True
            runs = [r for r in self.runs.values() if r.status != "done"]
        for run in runs:
            self.cancel(run.client.name, run.id)
        deadline = time.monotonic() + wait
        for thread in list(self.threads):
            thread.join(max(0, deadline - time.monotonic()))

    def reap(self) -> None:
        keep = self.broker().keep_finished_seconds
        now = self.clock()
        with self.cond:
            for key, run in list(self.runs.items()):
                if run.status == "done" and run.finished_at is not None and now - run.finished_at >= keep:
                    del self.runs[key]

    # --- the queue ------------------------------------------------------------------------

    def _next_up(self) -> Run | None:
        """The first queued run whose client has room, if there is room overall. (Lock held.)"""
        if sum(self.running.values()) >= self.broker().max_concurrent:
            return None
        for run in self.queue:
            if self.running.get(run.client.name, 0) < run.client.max_concurrent:
                return run
        return None

    def _wait_turn(self, run: Run) -> bool:
        """Wait until this run may start (True), or is cancelled while waiting (False)."""
        last = None
        with self.cond:
            while True:
                if run.cancel_requested or self.closed:
                    self.queue.remove(run)
                    self.cond.notify_all()
                    return False
                if self._next_up() is run:
                    self.queue.remove(run)
                    self.running[run.client.name] = self.running.get(run.client.name, 0) + 1
                    run.status = "running"
                    return True
                position = self.queue.index(run) + 1
                if position != last:
                    run.add({"type": "queued", "position": position})
                    last = position
                self.cond.wait(5)

    def _thread(self, run: Run) -> None:
        try:
            if self._wait_turn(run):
                try:
                    self._execute(run)
                finally:
                    with self.cond:
                        self.running[run.client.name] -= 1
                        if not self.running[run.client.name]:
                            del self.running[run.client.name]
                        self.cond.notify_all()
            else:
                run.add({"type": "error", "kind": "cancelled", "detail": "cancelled before it started"})
        except Exception:   # never leave a follower waiting on a run that died
            log.exception("run %s/%s failed", run.client.name, run.id)
            run.add({"type": "error", "kind": "internal", "detail": "the broker failed this run; see its log"})
        finally:
            run.finish()
            with self.cond:
                self.threads.discard(threading.current_thread())

    # --- running ----------------------------------------------------------------------------

    def _mcp_config(self, run: Run) -> Path | None:
        if not run.request.mcp_tools:
            return None
        argv, env = self.self_command
        config = {"mcpServers": {run.client.name: {
            "type": "stdio",
            "command": argv[0],
            "args": argv[1:] + ["mcp-proxy", "--socket", str(run.client.tools_socket())],
            "env": env,
        }}}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        path = self.work_dir / f"mcp-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(config))
        return path

    def _on_event(self, run: Run, event: dict) -> None:
        if event.get("type") == "rate_limits":
            self.rate_limits = event.get("rate_limits")
        run.add(event)

    def _execute(self, run: Run) -> None:
        req, client = run.request, run.client
        claude = self.broker().claude_bin()
        if not claude:
            run.add({"type": "error", "kind": "no_claude", "detail": "claude isn't installed for the broker's user"})
            return
        profile = self.profiles.get(client.name, req.profile)
        if not profile:
            run.add({"type": "error", "kind": "no_profile", "detail": f"no profile called {req.profile}"})
            return
        model = req.model or client.default_model or self.broker().default_model
        with self.profiles.using(client.name, req.profile):
            mcp_config = self._mcp_config(run)
            try:
                spec = RunSpec(
                    claude=claude, prompt=req.prompt, cwd=profile.path, session_id=req.session_id,
                    resume=req.resume, schema=req.schema, system_prompt_file=profile.system_prompt,
                    add_dirs=req.add_dirs, builtin_tools=req.builtin_tools, mcp_config=mcp_config,
                    mcp_tools=[f"mcp__{client.name}__{tool}" for tool in req.mcp_tools],
                    model=model or None, effort=req.effort or client.default_effort or None,
                    timeout=req.timeout)
                process = ClaudeProcess(spec, lambda e: self._on_event(run, e), FieldStream(req.stream_fields))
                with self.cond:
                    run.process = process
                    if run.cancel_requested:
                        process.kill(Cancelled("cancelled by the client"))
                run.add({"type": "started", "run_id": run.id, "session_id": req.session_id,
                         "resumed": req.resume, "model": model or None, "profile": req.profile})
                log.info("run %s/%s started (session %s)", client.name, run.id, req.session_id)
                try:
                    result = process.run()
                except RunFailed as e:
                    log.info("run %s/%s failed: %s: %s", client.name, run.id, e.kind, e.detail)
                    run.add({"type": "error", "kind": e.kind, "detail": e.detail})
                    return
                session_id = result.get("session_id") or req.session_id
                self.sessions.record(session_id, client.name, model, req.profile)
                structured = result.get("structured_output")
                if req.schema is not None and structured is None:
                    run.add({"type": "error", "kind": "schema", "session_id": session_id,
                             "detail": "claude finished without a reply that fits the schema"})
                    return
                run.add({
                    "type": "result",
                    "session_id": session_id,
                    "structured": structured,
                    "text": result.get("result"),
                    "context_tokens": process.output.context_tokens,
                    "usage": result.get("usage"),
                    "cost_usd": result.get("total_cost_usd"),
                    "duration_ms": result.get("duration_ms"),
                    "num_turns": result.get("num_turns"),
                    "rate_limits": self.rate_limits,
                })
                log.info("run %s/%s done", client.name, run.id)
            finally:
                if mcp_config:
                    mcp_config.unlink(missing_ok=True)
