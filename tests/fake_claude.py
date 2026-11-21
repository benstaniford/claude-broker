"""A stand-in for `claude -p ... --output-format stream-json`: what the broker reads.

FAKE_CLAUDE_LOG    a file each call's argv, cwd and chosen env are appended to (JSON lines)
FAKE_CLAUDE_MODE   ok (default) | fail | missing | hang | noschema | crash-after-stream
FAKE_SLEEP         seconds to wait before the result (queue tests)
FAKE_REPLY         the structured reply (JSON); default {"narration": ..., "scene": ...}
FAKE_RATE_LIMITS   a rate_limit_info object to report first
FAKE_RETRY         1: write one StructuredOutput call, then start again (a schema retry)
FAKE_MCP_CALL      a JSON-RPC line to send through the --mcp-config server; its reply is the result text
"""

import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "env": {
        k: os.environ.get(k) for k in ("ENABLE_CLAUDEAI_MCP_SERVERS", "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS",
                                       "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_BROKER_STATE")}}) + "\n")

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "missing" and "--resume" in args:
    print("No conversation found with session ID: x", file=sys.stderr)
    sys.exit(1)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(120)

sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]


def emit(msg):
    print(json.dumps(msg), flush=True)


def event(e):
    emit({"type": "stream_event", "event": e})


emit({"type": "system", "subtype": "init", "session_id": sid})
if os.environ.get("FAKE_RATE_LIMITS"):
    emit({"type": "rate_limit_event", "rate_limit_info": json.loads(os.environ["FAKE_RATE_LIMITS"])})

reply = json.loads(os.environ.get("FAKE_REPLY") or json.dumps(
    {"narration": "Arrows hiss from the brush.", "scene": "goblins in a thicket", "n": 3}))
text = json.dumps(reply)
for attempt in range(2 if os.environ.get("FAKE_RETRY") else 1):
    event({"type": "content_block_start", "index": 1,
           "content_block": {"type": "tool_use", "name": "StructuredOutput", "input": {}}})
    for i in range(0, len(text), 7):
        event({"type": "content_block_delta", "index": 1,
               "delta": {"type": "input_json_delta", "partial_json": text[i:i + 7]}})

emit({"type": "assistant", "message": {
    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/notes.md"}},
                {"type": "tool_use", "name": "StructuredOutput", "input": reply}],
    "usage": {"input_tokens": 10, "cache_read_input_tokens": 1200, "cache_creation_input_tokens": 30}}})

if mode == "crash-after-stream":
    print("killed", file=sys.stderr)
    sys.exit(137)

result_text = "done"
if os.environ.get("FAKE_MCP_CALL"):
    config = json.load(open(args[args.index("--mcp-config") + 1]))
    server = next(iter(config["mcpServers"].values()))
    env = dict(os.environ, **server.get("env", {}))
    proc = subprocess.run([server["command"], *server["args"]], input=os.environ["FAKE_MCP_CALL"] + "\n",
                          capture_output=True, text=True, env=env, timeout=20)
    result_text = proc.stdout.strip() or ("proxy failed: " + proc.stderr.strip())

if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))

result = {"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": result_text,
          "num_turns": 2, "duration_ms": 1234, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "output_tokens": 50}}
if mode != "noschema" and "--json-schema" in args:
    result["structured_output"] = reply
emit(result)
