# CLAUDE.md

Guide for Claude Code in this repo.

## Project

claude-broker: a systemd user service, shipped as a `.deb`, that runs `claude -p` for client apps
(Docker containers) over one unix socket per client. It is generic: apps bring their own prompts,
schemas, text-only profiles and (through an MCP server in their container) tools. Born from the
Dungeons & Claude DM bridge (`~/Code/DandD/scripts/dm-bridge.py`, issue DandD#56). The README is
the user-facing reference for the API, events, config and security model; keep it in step.

## Commands

```bash
python3 -m unittest discover -s tests -t . -v     # all tests
python3 -m claude_broker <command>                 # run from the checkout
scripts/build-deb <version> [out-dir]              # the .deb (zipapp + user unit + docs)
scripts/make-release                               # push the next vX.Y.Z tag → .github/workflows/release.yml
```

## Layout

- `claude_broker/claude.py`: the locked-down command (`build_command`, `allowed_tools`,
  `locked_down`), `run_env`, `ClaudeProcess` (own process group, timeout, kill), `Output`
  (stream-json lines → events).
- `fields.py`: `scan` and `FieldStream`: top-level string fields of the StructuredOutput call while
  it streams (`field` deltas, `field_closed`, `fields_reset`).
- `config.py`: `broker.toml`, `clients.d/*.toml`, paths (`CLAUDE_BROKER_CONFIG/STATE/RUNTIME`
  override them; tests do), `ClientConfig.root_for`.
- `profiles.py`: upload checks (text only, fixed allowlist), fixed path, swap by rename, old copy
  kept while a run uses it (`using`).
- `sessions.py`: session → client and model (no cross-client resume, no model change).
- `runs.py`: `Registry`: idempotent `run_id`, event log with `seq`, followers, FIFO queue with global
  and per-client caps, cancel, reap.
- `api.py`: `validate_run` (every request check lives here). `server.py`: `Handler` (routes),
  `ClientServer` (one socket each), `Broker` (reload on SIGHUP, shutdown), `self_command`.
- `mcp_proxy.py`: stdio ↔ `tools.sock` byte relay.
- `client.py`, `fake_broker.py`: **stdlib only, no package imports**: apps copy them. Keep it so.
- `cli.py`: `serve`, `setup`, `check`, `client add|list|remove`, `call`, `profile put|list`, `mcp-proxy`.

## Rules

- Standard library only, Python ≥ 3.12 (the deb is `Architecture: all`).
- The broker never runs client-supplied code, never offers Bash, never loads the user's settings,
  hooks or MCP servers. Any new feature must keep that.
- Error kinds and event types are API: add, don't rename.
- Tests use `tests/fake_claude.py` (modes via `FAKE_*` env) and real sockets under `/tmp` (socket
  paths must stay under 108 bytes).
- Commit straight to `main` once tests pass.
