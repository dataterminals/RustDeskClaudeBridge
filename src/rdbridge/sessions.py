"""Opening RustDesk sessions.

Every session kind is opened the same way: ``RustDesk.exe --<verb> <peer-id>``.

Three things about that are easy to get wrong.

**No credential is involved.** For a peer RustDesk has already saved, the
password is in its own encrypted store and it authenticates from there. The
bridge names the peer and nothing else. RustDesk's CLI *does* accept
``--password``, and the bridge will not use it -- see ``policy.FORBIDDEN_ARGS``.

**The process you launch is not the session.** ``core_main_invoke_new_connection``
turns the verb into a ``rustdesk://`` URI and posts it to the already-running
instance through a window message; the process you started then exits. So a zero
exit code means "the request was handed over", not "connected".

**Therefore success has to be observed, not assumed.** ``open_session`` waits for
a matching session window to appear and reports what actually happened, rather
than reporting the launcher's exit code as if it meant something.
"""

import time

from . import windows
from .errors import BridgeError


def open_session(rd, policy, peer, kind="remote-desktop", wait_seconds=20.0):
    """Ask RustDesk to open a session, then wait to see whether one appeared."""
    verb = policy.check_session(kind)

    before = {w["hwnd"] for w in windows.session_windows()}
    rd.run([verb, peer.id], check=False)

    expected = windows.window_kind(kind)
    deadline = time.monotonic() + float(wait_seconds)
    appeared = None
    while time.monotonic() < deadline:
        for window in windows.session_windows():
            if window["hwnd"] in before:
                continue
            if window.get("kind") == expected or kind == "rdp":
                appeared = window
                break
        if appeared:
            break
        time.sleep(0.4)

    result = {
        "peer": peer.label,
        "peer_id": peer.id,
        "kind": kind,
        "verb": verb,
        "credential_used": None,
        "opened": bool(appeared),
    }
    if appeared:
        result["window"] = {
            "hwnd": appeared["hwnd"],
            "title": appeared["title"],
            "minimized": appeared["minimized"],
            "client_size": appeared["client_size"],
        }
    else:
        existing = [w["title"] for w in windows.session_windows()]
        result["note"] = (
            "RustDesk accepted the request but no new %s window appeared within "
            "%ss. It may still be connecting, the peer may be offline, or the "
            "session may have been refused. Open windows now: %s"
            % (kind, wait_seconds, ", ".join(existing) or "none")
        )
    return result


def close_session(peer=None, kind=None):
    """Close one session window.

    Sends WM_CLOSE, which is what clicking the X does -- RustDesk gets to run its
    own teardown, including honouring 'lock after session end'. Killing the
    process instead would skip that.

    "Closed" means the window is no longer visible, not that its handle is gone:
    RustDesk hides session windows rather than destroying them, so waiting for
    the HWND to disappear would time out on every successful close.
    """
    import ctypes

    window = windows.find_session_window(peer=peer, kind=kind)
    WM_CLOSE = 0x0010
    ctypes.windll.user32.PostMessageW(window["hwnd"], WM_CLOSE, 0, 0)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        still_open = {w["hwnd"] for w in windows.session_windows()}
        if window["hwnd"] not in still_open:
            return {"closed": True, "title": window["title"]}
        time.sleep(0.3)
    raise BridgeError(
        "Sent WM_CLOSE to '%s' but the window is still open after 10s. It may be "
        "showing a confirmation dialog." % window["title"]
    )


def list_sessions():
    """Every open session window, as the bridge sees it."""
    listed = []
    for window in windows.session_windows():
        listed.append({
            "title": window["title"],
            "peer_label": window.get("peer_label"),
            "hostname": window.get("hostname"),
            "kind": window.get("kind"),
            "minimized": window["minimized"],
            "client_size": window["client_size"],
            "hwnd": window["hwnd"],
        })
    return listed
