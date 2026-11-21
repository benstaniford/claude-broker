# claude-broker

Claude Code as a local service for containers.

The broker runs `claude -p` as you, with your Claude login and plan, and serves client apps,
usually Docker containers, over **one unix socket per client**. An app sends a prompt, a JSON
schema and its own text-only *profile* (system prompt, CLAUDE.md, skills). The reply streams
back as NDJSON, including the fields of the structured reply while Claude is still writing them.

The broker knows nothing about any app. It never runs an app's code: an app's tools are an
MCP server inside its own container, which the broker relays to.

```
┌─ container ──────────────────────┐        ┌─ host, as you ──────────────────────────┐
│ app ── claude_broker_client.py ──┼─ broker.sock ─▶ claude-broker ──▶ claude -p ...   │
│ app's MCP server ◀───────────────┼─ tools.sock ◀── claude-broker mcp-proxy ◀─┘       │
└──────────────────────────────────┘        └─────────────────────────────────────────┘
        both sockets live in /run/user/<uid>/claude-broker/<client>/, mounted into the container
```

## Install

Needs Debian or Ubuntu with Python 3.12 or later, and [Claude Code](https://docs.claude.com/en/docs/claude-code)
installed and logged in for your user (run `claude` once and sign in).

```bash
sudo apt install ./claude-broker_X.Y.Z_all.deb     # from the GitHub release
claude-broker setup
```

`setup` does four things:
- writes `~/.config/claude-broker/broker.toml`
- turns on linger (`loginctl enable-linger`), so the service runs while you're logged out
- enables and starts the systemd **user** service `claude-broker`
- runs `claude-broker check`

Logs: `journalctl --user -u claude-broker -f`.

## Add a client

```bash
claude-broker client add myapp \
    --root ~/myapp/data \              # folders runs may read and write (repeat)
    --read-only ~/myapp/library \      # folders runs may only read (repeat)
    --model claude-sonnet-5-5          # models it may use (repeat; none means any)
```

This writes `~/.config/claude-broker/clients.d/myapp.toml` and reloads the broker. The client's
socket is `/run/user/<uid>/claude-broker/myapp/broker.sock`.

Mount the client's **folder**, not the socket file, into the container. The broker re-creates
its socket on every restart, and a mounted file would go stale.

```yaml
services:
  myapp:
    user: "1000:1000"                     # the broker's user: the socket is 0600
    volumes:
      - /run/user/1000/claude-broker/myapp:/run/claude-broker
      - ./data:/app/data                  # the same folders as the client's roots
```

**Folder paths in requests are host paths.** `claude` runs on the host, so `add_dirs` names
folders as the host sees them. Mount them into the container too if the app reads or writes them.

### Client settings (`clients.d/<name>.toml`)

| Setting | Default | |
|---|---|---|
| `workspace_roots` | `[]` | folders runs may read and write |
| `read_only_roots` | `[]` | folders runs may only read (the most specific root wins) |
| `models` | `[]` | allowed models; empty means any |
| `default_model`, `default_effort` | `""` | used when a run doesn't say |
| `max_concurrent` | `1` | this client's runs at once |
| `max_run_seconds` | `900` | the longest a run may take, and the default timeout |
| `allowed_builtin_tools` | all six | out of `Read Grep Glob Edit Write Skill` |

### Broker settings (`broker.toml`)

| Setting | Default | |
|---|---|---|
| `claude` | found on `PATH` or `~/.local/bin` | the claude binary |
| `max_concurrent` | `2` | runs at once over **all** clients (one plan's limits) |
| `keep_finished_seconds` | `1800` | how long a finished run can be rejoined |
| `default_model` | `""` | |
| `log_level` | `info` | |

`systemctl --user reload claude-broker` (SIGHUP) re-reads both. Clients added or removed start
or stop listening at once.

## Use it from an app

Copy [`claude_broker/client.py`](claude_broker/client.py) into your app as
`claude_broker_client.py`. It's one file and uses only the standard library. The package also
installs it in `/usr/share/doc/claude-broker/`.

```python
from claude_broker_client import BrokerClient, BrokerError

broker = BrokerClient("/run/claude-broker/broker.sock", timeout=60)
broker.put_profile("helper", "/app/profile")         # cheap when nothing changed

session = None
for event in broker.run(
        "Summarise chapter 3.",
        run_id="job-42",                              # resubmitting the same id never runs twice
        profile="helper",
        session={"resume": session} if session else None,
        schema={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
        add_dirs=[{"path": "/home/me/myapp/library/book-1", "write": False}],
        stream_fields=["summary"]):
    if event["type"] == "field":
        print(event.get("delta", ""), end="", flush=True)
    elif event["type"] == "result":
        session = event["session_id"]
        reply = event["structured"]
    elif event["type"] == "error":
        raise BrokerError(event["detail"], kind=event["kind"])
```

For tests, [`claude_broker/fake_broker.py`](claude_broker/fake_broker.py) is a stand-in broker
on a socket that replays the events you script. It's also one stdlib-only file.

From a shell:

```bash
curl --unix-socket /run/user/1000/claude-broker/myapp/broker.sock http://broker/v1/health
claude-broker call --client myapp "Say hello" --schema '{"type":"object","properties":{"text":{"type":"string"}}}' --field text
```

## API

HTTP/1.1 over the client's socket. JSON errors are `{"errors": ["..."]}`: 400 for a bad request,
404 for an unknown run or profile, 503 for a removed client or a broker that's stopping.

| | |
|---|---|
| `GET /v1/health` | version, claude's path, whether `tools.sock` is there, running and queued counts |
| `GET /v1/usage` | `{"rate_limits": ...}`: the plan's latest limits (shared by all clients) |
| `GET /v1/runs` | this client's known runs |
| `POST /v1/runs` | start a run, or rejoin the one with this `run_id`: an NDJSON stream |
| `GET /v1/runs/<id>?from=<seq>` | rejoin a run from event `seq`: an NDJSON stream |
| `DELETE /v1/runs/<id>` | cancel it (kills claude and everything it started) |
| `GET /v1/profiles` | `[{name, sha256, files}]` |
| `PUT /v1/profiles/<name>` | body: a tar or tar.gz of the profile |
| `DELETE /v1/profiles/<name>` | |

### `POST /v1/runs`

| Field | | |
|---|---|---|
| `prompt` | required | |
| `run_id` | a new id | 1–128 of `A-Za-z0-9._:-`. Running or recently finished: you get the same run |
| `profile` | an empty folder | an uploaded profile's name: the run's working folder |
| `session` | `{"new": true}` | or `{"resume": "<session_id>"}` from an earlier result: same client, model and profile only |
| `schema` | none | a JSON schema (at most 64 KB): the reply comes as `structured` |
| `model`, `effort` | the client's defaults | `effort`: `low medium high xhigh max` |
| `add_dirs` | `[]` | `[{"path": "/host/folder", "write": false}]` (or just the path), under the client's roots |
| `builtin_tools` | Read, Grep, Glob (plus Edit, Write if any folder is writable) | out of the client's allowed tools. Bash is never offered |
| `mcp_tools` | `[]` | tool names served by the client's `tools.sock`; claude sees them as `mcp__<client>__<name>` |
| `stream_fields` | `[]` | top-level string fields of the structured reply to stream while they're written |
| `timeout` | `max_run_seconds` | seconds |

### Events

Each event is one JSON line with `type` and `seq`, its place in the run. A run keeps all its
events, so rejoining replays them before the live ones arrive.

| `type` | |
|---|---|
| `queued` | `position` in the queue (sent when it changes) |
| `started` | `run_id`, `session_id`, `resumed`, `model`, `profile` |
| `field` | `name`, and either `delta` (text to append) or `text` (replaces what you have) |
| `field_closed` | `name`, `text`: the field is complete |
| `fields_reset` | claude started its reply again (after a schema error): clear streamed fields |
| `tool` | `name`, `input` (or `input_truncated`): a tool claude called |
| `rate_limits` | `rate_limits`: the plan's limits, as claude reported them |
| `result` | `session_id`, `structured`, `text`, `context_tokens`, `usage`, `cost_usd`, `duration_ms`, `num_turns`, `rate_limits` |
| `error` | `kind`, `detail`. Kinds: `claude_exit`, `session_missing`, `timeout`, `cancelled`, `schema`, `no_claude`, `no_profile`, `internal` |
| `ping` | every 15 s of quiet, so a slow run can't be mistaken for a dead socket (the client library drops these) |

Your app decides when a session is too long, using `context_tokens`. Start a new session (and
give it whatever context it needs) when that gets large. Keep one model per session: the prompt
cache is per model, so the broker refuses a model change on `resume`.

## Profiles

A profile is the folder a run starts in, uploaded as a tarball:

```
SYSTEM.md                   appended to Claude's system prompt
CLAUDE.md                   project memory
.claude/skills/<skill>/...  skills: .md .txt .json .csv .yaml .yml only
```

Anything else is refused: settings, hooks, MCP config, scripts, links, binary files. The broker
drops execute bits, caps a profile at 20 MB and 2,000 files, and keeps each profile at a fixed
path. Claude files sessions by folder, so `resume` keeps working across uploads. A run already
going keeps the copy it started with.

## Tools: an MCP server in your container

To give Claude your app's own tools, serve MCP (newline-delimited JSON-RPC, as over stdio) on a
unix socket called `tools.sock` in the mounted folder, then list the tool names in `mcp_tools`.
For each run the broker starts `claude-broker mcp-proxy`, which relays claude's stdio to your
socket. Your tools run inside your container and nothing of yours runs on the host. Each
connection to `tools.sock` is one MCP session, beginning with `initialize`.

## Security model

- **Who can use a client:** whoever can open its socket. The folder is `0700` and the socket
  `0600`, owned by the broker's user. Mounting a client's folder into a container gives that
  container that client, and nothing else.
- **What a run can touch:** its profile and the `add_dirs` it asked for (checked against the
  client's roots after following symlinks), with only the built-in tools asked for. Edit and
  Write apply only to writable folders. Every run gets:
  - `--setting-sources project`, so your own settings, permissions and hooks are never loaded
  - `--strict-mcp-config`, so only the client's relayed MCP server is available
  - `--permission-mode dontAsk`
  - the prompt on stdin, never on the command line, so it can't be read as an option
  - no ancestor `CLAUDE.md`
  - no claude.ai connectors, bundled skills or auto-memory
- **What the broker runs:** `claude`, and its own `mcp-proxy`. Never anything a client sent.
- Claude still runs as you. Treat a client's prompts like any other input you give Claude
  Code, and keep its roots to the folders it needs.

## Development

```bash
python3 -m unittest discover -s tests -t . -v     # all tests (a fake claude; real sockets in /tmp)
python3 -m claude_broker --help                   # run from the checkout
scripts/build-deb 0.0.0                           # → dist/claude-broker_0.0.0_all.deb
scripts/make-release                              # tag the next vX.Y.Z; the Release workflow builds and publishes the .deb
```
