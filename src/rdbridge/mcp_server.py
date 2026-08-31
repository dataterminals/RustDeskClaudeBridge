"""MCP stdio server exposing the bridge as tools.

Speaks JSON-RPC 2.0 over newline-delimited stdin/stdout, using only the standard
library. Register it with::

    claude mcp add rustdesk --scope user -e "PYTHONPATH=<repo>/src" -- python -m rdbridge.mcp_server

The package is not installed, so ``src/`` has to reach the subprocess on
PYTHONPATH or this module cannot be imported at all. In Windows PowerShell,
quote the separator as ``'--'`` -- ``claude`` is a .ps1 shim and the parameter
binder eats a bare one. See the README for the full explanation.

Nothing in this module may print to stdout except protocol messages -- stdout is
the transport. Diagnostics go to stderr.

Tool descriptions say plainly which layer a tool belongs to, because the layers
differ enormously in how much they can surprise you: reading peers is free,
opening a session contacts another machine, and clicking moves a real cursor on
someone else's desktop with no undo.
"""

import json
import os
import sys
import traceback

from . import __version__
from .errors import BridgeError
from .ops import Bridge

DEFAULT_PROTOCOL = "2025-06-18"

_bridge = None


def bridge():
    """Build the Bridge lazily so a config error is reported as a tool error."""
    global _bridge
    if _bridge is None:
        _bridge = Bridge.from_config()
    return _bridge


_PEER = {"type": "string",
         "description": "Peer ID, alias or hostname. Omit to use the configured "
                        "default."}
_KIND = {"type": "string",
         "enum": ["remote-desktop", "file-transfer", "terminal", "view-camera",
                  "port-forward", "terminal-admin", "rdp"],
         "description": "Session kind."}

TOOLS = [
    # -- read-only ---------------------------------------------------------
    {
        "name": "rustdesk_status",
        "description": "READ-ONLY. RustDesk's state at a glance: this machine's "
                       "ID and version, saved peer count, open session windows, "
                       "and the remote capabilities last reported.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.status(),
    },
    {
        "name": "rustdesk_peers",
        "description": "READ-ONLY. Saved peers with alias, hostname, platform and "
                       "per-peer settings. Passwords are never read or returned; "
                       "has_saved_password only reports whether one exists.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.list_peers(),
    },
    {
        "name": "rustdesk_sessions",
        "description": "READ-ONLY. Session windows currently open on this machine, "
                       "with their peer, kind, and whether they are minimized.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.sessions(),
    },
    {
        "name": "rustdesk_peer_info",
        "description": "READ-ONLY. What the remote side reported on connect: its "
                       "RustDesk version, whether it supports the remote terminal, "
                       "and the geometry of each of its displays. Parsed from the "
                       "log, so it needs a session to have connected at least once.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.peer_info(),
    },
    {
        "name": "rustdesk_log",
        "description": "READ-ONLY. Recent notable RustDesk log events -- connects, "
                       "disconnects, auth failures. This is how to tell whether a "
                       "session actually connected, since opening one only means "
                       "the request was handed over.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer",
                                     "description": "Events to return (default 20)"}},
        },
        "handler": lambda b, a: b.log_events(limit=a.get("limit", 20)),
    },
    {
        "name": "rustdesk_options",
        "description": "READ-ONLY. Global RustDesk options, read from its config "
                       "file. Credential-bearing keys are filtered out. Reading via "
                       "RustDesk's own --option would require elevation even for a "
                       "read, so the file is used instead.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.global_options(),
    },
    {
        "name": "rustdesk_policy",
        "description": "READ-ONLY, no side effects. The active safety boundary: "
                       "which verbs can never run, which session kinds are allowed, "
                       "and whether option writes, forwards and input are enabled. "
                       "Check here before assuming an action is available.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.policy.explain(),
    },
    {
        "name": "rustdesk_doctor",
        "description": "READ-ONLY. End-to-end check of the setup: executable, "
                       "version, saved peers, whether a credential-free connect is "
                       "possible, log, and remote capabilities.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": lambda b, a: b.doctor(),
    },

    # -- sessions ----------------------------------------------------------
    {
        "name": "rustdesk_open_session",
        "description": "CONTACTS ANOTHER MACHINE. Open a RustDesk session with a "
                       "saved peer. No credential is passed: RustDesk authenticates "
                       "from its own store. Waits and reports whether a session "
                       "window actually appeared. 'terminal' gives a real shell on "
                       "the far machine and is usually a better tool than the "
                       "remote desktop.",
        "inputSchema": {
            "type": "object",
            "properties": {"peer": _PEER, "kind": _KIND,
                           "wait_seconds": {"type": "number"}},
        },
        "handler": lambda b, a: b.open(a.get("kind", "remote-desktop"),
                                       peer=a.get("peer"),
                                       wait_seconds=a.get("wait_seconds", 20.0)),
    },
    {
        "name": "rustdesk_close_session",
        "description": "Close a session window with WM_CLOSE, so RustDesk runs its "
                       "own teardown (including 'lock after session end').",
        "inputSchema": {
            "type": "object",
            "properties": {"peer": _PEER, "kind": _KIND},
        },
        "handler": lambda b, a: b.close(peer=a.get("peer"), kind=a.get("kind")),
    },

    # -- settings ----------------------------------------------------------
    {
        "name": "rustdesk_get_option",
        "description": "READ-ONLY. One global option, by key.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        "handler": lambda b, a: {a["key"]: b.get_option(a["key"])},
    },
    {
        "name": "rustdesk_set_option",
        "description": "PROMPTS FOR UAC. Write a global RustDesk option. RustDesk "
                       "requires administrator rights for this, so a consent prompt "
                       "appears on this machine every time. Governed by "
                       "options.allow_write in bridge config.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
        },
        "handler": lambda b, a: b.set_option(a["key"], a["value"]),
    },
    {
        "name": "rustdesk_get_peer_setting",
        "description": "READ-ONLY. One per-peer setting (image quality, fps, "
                       "keyboard mode and similar).",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "peer": _PEER},
            "required": ["key"],
        },
        "handler": lambda b, a: {a["key"]: b.get_peer_setting(a["key"],
                                                              peer=a.get("peer"))},
    },
    {
        "name": "rustdesk_set_peer_setting",
        "description": "Writes a per-peer setting into RustDesk's peer config file. "
                       "Refuses while a session is open, because RustDesk writes its "
                       "in-memory config back on close and would discard the edit. "
                       "Applies to the next session, not a running one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
                "peer": _PEER,
                "force": {"type": "boolean",
                          "description": "Edit even with a session open. Likely to "
                                         "be overwritten."},
            },
            "required": ["key", "value"],
        },
        "handler": lambda b, a: b.set_peer_setting(a["key"], a["value"],
                                                   peer=a.get("peer"),
                                                   force=bool(a.get("force"))),
    },

    # -- forwards ----------------------------------------------------------
    {
        "name": "rustdesk_forwards",
        "description": "READ-ONLY. Port forwards saved for a peer, as "
                       "[local_port, remote_host, remote_port].",
        "inputSchema": {"type": "object", "properties": {"peer": _PEER}},
        "handler": lambda b, a: b.forwards(peer=a.get("peer")),
    },
    {
        "name": "rustdesk_open_forward",
        "description": "OPENS A NETWORK PATH TO YOUR WHOLE LAN. Seed port "
                       "forwards into the peer config and open the port-forward "
                       "session, then report which local ports are listening. A "
                       "forward tunnels TCP to any host reachable from the remote "
                       "machine, which turns remote work into ordinary local "
                       "tooling. But RustDesk binds forwards to 0.0.0.0, not "
                       "127.0.0.1, so the tunnel is reachable by anything on the "
                       "local network -- close the session when done. Every "
                       "forward must be listed in forwards.allow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "forwards": {
                    "type": "array",
                    "description": "[[local_port, remote_host, remote_port], ...]",
                    "items": {"type": "array"},
                },
                "peer": _PEER,
                "force": {"type": "boolean"},
            },
        },
        "handler": lambda b, a: b.open_forward(forwards=a.get("forwards"),
                                               peer=a.get("peer"),
                                               force=bool(a.get("force"))),
    },

    # -- pixels ------------------------------------------------------------
    {
        "name": "rustdesk_geometry",
        "description": "READ-ONLY. How remote pixels map onto this screen: the "
                       "session window's client area, the remote display geometry, "
                       "and the current mapping. Reports whether the mapping is a "
                       "calibrated measurement or an aspect-preserving guess. Check "
                       "this before clicking.",
        "inputSchema": {
            "type": "object",
            "properties": {"peer": _PEER, "kind": _KIND,
                           "display": {"type": "integer"}},
        },
        "handler": lambda b, a: b.geometry_report(peer=a.get("peer"),
                                                  kind=a.get("kind", "remote-desktop"),
                                                  display=a.get("display")),
    },
    {
        "name": "rustdesk_calibrate",
        "description": "Store an exact remote-to-screen mapping from at least two "
                       "point correspondences, measured by looking at a screenshot. "
                       "Stored client-relative, so moving the window keeps it valid; "
                       "resizing or scrolling does not.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pairs": {
                    "type": "array",
                    "description": "[[[remote_x, remote_y], [screen_x, screen_y]], ...]",
                    "items": {"type": "array"},
                },
                "peer": _PEER,
                "kind": _KIND,
                "display": {"type": "integer"},
            },
            "required": ["pairs"],
        },
        "handler": lambda b, a: b.calibrate(
            [((p[0][0], p[0][1]), (p[1][0], p[1][1])) for p in a["pairs"]],
            peer=a.get("peer"), kind=a.get("kind", "remote-desktop"),
            display=a.get("display")),
    },
    {
        "name": "rustdesk_click",
        "description": "SENDS REAL INPUT TO ANOTHER MACHINE, NO UNDO. Clicks at a "
                       "remote coordinate by focusing the session window and moving "
                       "this machine's actual cursor. The remote desktop is a video "
                       "stream with no element handles, so this is blind clicking -- "
                       "look at a screenshot first, and prefer the remote terminal "
                       "or a port forward whenever the task allows it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Remote display X"},
                "y": {"type": "integer", "description": "Remote display Y"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "double": {"type": "boolean"},
                "peer": _PEER,
                "display": {"type": "integer"},
            },
            "required": ["x", "y"],
        },
        "handler": lambda b, a: b.remote_click(a["x"], a["y"],
                                               button=a.get("button", "left"),
                                               double=bool(a.get("double")),
                                               peer=a.get("peer"),
                                               display=a.get("display")),
    },
    {
        "name": "rustdesk_scroll",
        "description": "SENDS REAL INPUT TO ANOTHER MACHINE. Scrolls at a remote "
                       "coordinate; positive clicks scroll up.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "clicks": {"type": "integer"},
                "peer": _PEER, "display": {"type": "integer"},
            },
            "required": ["x", "y", "clicks"],
        },
        "handler": lambda b, a: b.remote_scroll(a["x"], a["y"], a["clicks"],
                                                peer=a.get("peer"),
                                                display=a.get("display")),
    },
    {
        "name": "rustdesk_type",
        "description": "SENDS REAL KEYSTROKES TO ANOTHER MACHINE, NO UNDO. Types "
                       "text into whatever has focus on the remote side. Never use "
                       "for passwords or any other credential.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "peer": _PEER},
            "required": ["text"],
        },
        "handler": lambda b, a: b.remote_type(a["text"], peer=a.get("peer")),
    },
    {
        "name": "rustdesk_press",
        "description": "SENDS A REAL KEYSTROKE TO ANOTHER MACHINE. One named key: "
                       "enter, tab, escape, arrows, function keys.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "peer": _PEER},
            "required": ["key"],
        },
        "handler": lambda b, a: b.remote_press(a["key"], peer=a.get("peer")),
    },
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _public_tools():
    return [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _content(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2,
                                                              default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(message):
    """Handle one JSON-RPC message; return a response dict, or None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "rustdesk-bridge", "version": __version__},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": _public_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return _result(request_id, _content("Unknown tool: %s" % name, True))
        try:
            return _result(request_id, _content(tool["handler"](bridge(), arguments)))
        except BridgeError as exc:
            # Policy refusals and RustDesk failures are normal outcomes.
            return _result(request_id, _content(
                "%s: %s" % (type(exc).__name__, exc), True))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _result(request_id, _content(
                "Unexpected %s: %s" % (type(exc).__name__, exc), True))

    if request_id is None:
        return None
    return _error(request_id, -32601, "Method not found: %s" % method)


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        # A UTF-8 BOM on the first message is not valid JSON, and some launchers
        # prepend one; that would fail the initialize handshake and nothing else.
        line = line.lstrip("﻿").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "Parse error")) + "\n")
            stdout.flush()
            continue

        response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")  # tolerate a leading BOM
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.write("rustdesk-bridge %s ready (cwd %s)\n" % (__version__, os.getcwd()))
    try:
        serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
