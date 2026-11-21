"""Checking what clients send: a run's body becomes a RunRequest, or a list of errors."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .claude import BUILTIN_TOOLS, EFFORTS, READ_TOOLS, WRITE_TOOLS
from .config import BrokerConfig, ClientConfig
from .profiles import DEFAULT, ProfileStore
from .runs import RunRequest
from .sessions import SessionStore

RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SESSION_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TOOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FIELD_RE = re.compile(r"^.{1,100}$", re.S)
MAX_PROMPT = 1024 * 1024
MAX_SCHEMA = 64 * 1024          # it goes on claude's command line: well under the kernel's per-argument limit
MAX_FIELDS = 20
MAX_DIRS = 20
MAX_MCP_TOOLS = 100
KNOWN = {"run_id", "prompt", "profile", "session", "schema", "model", "effort", "add_dirs",
         "builtin_tools", "mcp_tools", "stream_fields", "timeout"}


def validate_run(body, client: ClientConfig, broker: BrokerConfig, profiles: ProfileStore,
                 sessions: SessionStore) -> tuple[RunRequest | None, list[str]]:
    if not isinstance(body, dict):
        return None, ["the body must be a JSON object"]
    errors: list[str] = []
    unknown = set(body) - KNOWN
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")

    run_id = body.get("run_id") or uuid.uuid4().hex
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        errors.append("run_id: 1–128 letters, digits, . _ : or -")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("prompt: required, a non-empty string")
    elif len(prompt.encode()) > MAX_PROMPT:
        errors.append("prompt: larger than 1 MB")

    profile = body.get("profile") or DEFAULT
    if not isinstance(profile, str) or (profile != DEFAULT and not profiles.get(client.name, profile)):
        errors.append(f"profile: no profile called {profile!r} (PUT /v1/profiles/<name> first)")

    model = body.get("model") or client.default_model or broker.default_model
    if not isinstance(model, str):
        errors.append("model: a string")
        model = ""
    elif client.models and model not in client.models:
        errors.append(f"model: {model or '(default)'} isn't allowed for this client "
                      f"(allowed: {', '.join(client.models)})")

    session = body.get("session") or {"new": True}
    session_id, resume = str(uuid.uuid4()), False
    if not isinstance(session, dict):
        errors.append('session: {"new": true} or {"resume": "<session id>"}')
    elif "resume" in session:
        session_id, resume = session.get("resume"), True
        if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
            errors.append("session.resume: a session id (a UUID) from an earlier result")
        else:
            why = sessions.check(session_id, client.name, model, profile if isinstance(profile, str) else None)
            if why:
                errors.append(f"session.resume: {why}")
    elif session.get("new") is not True:
        errors.append('session: {"new": true} or {"resume": "<session id>"}')

    schema = body.get("schema")
    if schema is not None and not isinstance(schema, dict):
        errors.append("schema: a JSON schema object")
    elif schema is not None and len(json.dumps(schema).encode()) > MAX_SCHEMA:
        errors.append(f"schema: larger than {MAX_SCHEMA // 1024} KB")

    effort = body.get("effort") or ""
    if effort and effort not in EFFORTS:
        errors.append(f"effort: one of {', '.join(EFFORTS)}")

    add_dirs: list[tuple[Path, bool]] = []
    raw_dirs = body.get("add_dirs") or []
    if not isinstance(raw_dirs, list) or len(raw_dirs) > MAX_DIRS:
        errors.append(f"add_dirs: a list of at most {MAX_DIRS} {{path, write}} objects")
        raw_dirs = []
    for entry in raw_dirs:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            errors.append("add_dirs: each entry is {\"path\": \"/abs/folder\", \"write\": false}")
            continue
        raw, write = entry["path"], entry.get("write", False) is True
        if not Path(raw).is_absolute():
            errors.append(f"add_dirs: {raw} is not an absolute path")
            continue
        path = Path(raw).resolve()
        if not path.is_dir():
            errors.append(f"add_dirs: {raw} is not a folder the broker can see "
                          f"(the path on the host, not inside the container)")
            continue
        root = client.root_for(path)
        if not root:
            errors.append(f"add_dirs: {raw} is outside this client's roots")
            continue
        if write and not root[1]:
            errors.append(f"add_dirs: {raw} is under a read-only root")
            continue
        add_dirs.append((path, write))

    tools = body.get("builtin_tools")
    if tools is None:
        tools = [t for t in READ_TOOLS if t in client.allowed_builtin_tools]
        if any(w for _, w in add_dirs):
            tools += [t for t in WRITE_TOOLS if t in client.allowed_builtin_tools]
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        errors.append("builtin_tools: a list of tool names")
        tools = []
    for tool in tools:
        if tool not in BUILTIN_TOOLS:
            errors.append(f"builtin_tools: {tool} isn't offered (only {', '.join(BUILTIN_TOOLS)})")
        elif tool not in client.allowed_builtin_tools:
            errors.append(f"builtin_tools: {tool} isn't allowed for this client")

    mcp_tools = body.get("mcp_tools") or []
    if not isinstance(mcp_tools, list) or len(mcp_tools) > MAX_MCP_TOOLS or \
            not all(isinstance(t, str) and TOOL_RE.match(t) for t in mcp_tools):
        errors.append("mcp_tools: a list of tool names (letters, digits, _ or -) from the client's MCP server")
        mcp_tools = []
    if mcp_tools and not client.tools_socket().exists():
        errors.append(f"mcp_tools: the client's MCP server isn't listening at {client.tools_socket()} "
                      f"(tools.sock in the mounted folder)")

    fields = body.get("stream_fields") or []
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS or \
            not all(isinstance(f, str) and FIELD_RE.match(f) for f in fields):
        errors.append(f"stream_fields: a list of at most {MAX_FIELDS} top-level string field names")
        fields = []

    timeout = body.get("timeout", client.max_run_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or \
            not 1 <= timeout <= client.max_run_seconds:
        errors.append(f"timeout: seconds, from 1 to {client.max_run_seconds}")
        timeout = client.max_run_seconds

    if errors:
        return None, errors
    return RunRequest(
        run_id=run_id, prompt=prompt, profile=profile, session_id=session_id, resume=resume,
        schema=schema, model=model, effort=effort, add_dirs=add_dirs, builtin_tools=list(dict.fromkeys(tools)),
        mcp_tools=list(dict.fromkeys(mcp_tools)), stream_fields=list(dict.fromkeys(fields)),
        timeout=float(timeout)), []
