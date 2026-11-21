"""`claude-broker mcp-proxy --socket PATH`: claude's stdio MCP server, relayed to a unix socket.

claude only starts MCP servers as processes (or reaches them over HTTP). A client's tools
are an MCP server inside its container, listening on `tools.sock` in the folder it mounts.
claude starts this relay, the broker's own code, and it passes bytes both ways untouched
(newline-delimited JSON-RPC), so no client code ever runs on the host.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import threading


def relay(path: str, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    # tools.sock sits in a folder the client's container can write: a symlink there could aim
    # this relay at any socket on the host, so only a socket itself is used.
    try:
        is_socket = stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        is_socket = False
    if not is_socket:
        print(f"claude-broker mcp-proxy: {path} isn't a unix socket (links aren't followed)", file=sys.stderr)
        return 1
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError as e:
        sock.close()
        print(f"claude-broker mcp-proxy: can't reach the client's MCP server at {path}: {e}", file=sys.stderr)
        return 1

    def upstream():
        try:
            while True:
                chunk = stdin.read1(65536) if hasattr(stdin, "read1") else os.read(stdin.fileno(), 65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            stdout.write(chunk)
            stdout.flush()
    except OSError:
        pass
    finally:
        sock.close()
    return 0
