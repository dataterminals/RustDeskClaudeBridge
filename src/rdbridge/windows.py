"""Finding RustDesk's windows on this machine.

Verified against RustDesk 1.4.5 on Windows 11:

* the main window is class ``FLUTTER_RUNNER_WIN32_WINDOW``, titled ``RustDesk``
* every session opens its own top-level window of class ``RustdeskMultiWindow``,
  titled ``<alias>@<hostname> - <Session Type> - RustDesk``

The title is the only place the session's peer and kind are exposed to the
outside, so it is what the bridge matches on.

One trap worth naming: a **minimized** window reports a rect at ``-32000,-32000``
rather than an error. Treating that as real coordinates would send clicks into
nowhere, so ``is_minimized`` is checked before any geometry is trusted.
"""

import ctypes
from ctypes import wintypes

from .errors import WindowNotFound

MAIN_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"
SESSION_WINDOW_CLASS = "RustdeskMultiWindow"

# Window titles use the app's display names for session kinds; map them back to
# the bridge's kind vocabulary.
_TITLE_KINDS = {
    "remote desktop": "remote-desktop",
    "file transfer": "file-transfer",
    "terminal": "terminal",
    "view camera": "view-camera",
    "port forward": "port-forward",
    "rdp": "rdp",
}

_user32 = ctypes.windll.user32
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _text(hwnd, getter, length_getter=None):
    length = length_getter(hwnd) + 1 if length_getter else 512
    buffer = ctypes.create_unicode_buffer(max(length, 2))
    getter(hwnd, buffer, len(buffer))
    return buffer.value


def _window_record(hwnd):
    class_name = _text(hwnd, _user32.GetClassNameW)
    title = _text(hwnd, _user32.GetWindowTextW, _user32.GetWindowTextLengthW)

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    rect = _RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    client = _RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(client))
    origin = _POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(origin))

    minimized = bool(_user32.IsIconic(hwnd))
    try:
        dpi = int(_user32.GetDpiForWindow(hwnd))
    except (AttributeError, OSError):
        dpi = 96

    return {
        "hwnd": int(hwnd),
        "pid": int(pid.value),
        "class": class_name,
        "title": title,
        "visible": bool(_user32.IsWindowVisible(hwnd)),
        "minimized": minimized,
        "dpi": dpi,
        "window_rect": [rect.left, rect.top, rect.right, rect.bottom],
        # Meaningless while minimized -- Windows parks the window off-screen at
        # -32000 instead of reporting an error.
        "client_size": [client.right - client.left, client.bottom - client.top],
        "client_origin": [origin.x, origin.y],
    }


def enumerate_windows(class_name=None):
    """All top-level windows, optionally filtered to one class."""
    found = []

    def callback(hwnd, _lparam):
        record = _window_record(hwnd)
        if class_name is None or record["class"] == class_name:
            found.append(record)
        return True

    _user32.EnumWindows(_ENUM_PROC(callback), 0)
    return found


def parse_session_title(title):
    """Split ``<alias>@<host> - <Kind> - RustDesk`` into its parts.

    Split from the right: an alias may itself contain ' - '.
    """
    parts = title.rsplit(" - ", 2)
    if len(parts) != 3 or parts[2] != "RustDesk":
        return {"peer_label": title, "hostname": None, "kind": None}
    who, kind_text = parts[0], parts[1]
    alias, _, hostname = who.rpartition("@")
    return {
        "peer_label": alias or who,
        "hostname": hostname or None,
        "kind": _TITLE_KINDS.get(kind_text.strip().lower()),
        "kind_text": kind_text,
    }


def session_windows(include_hidden=False):
    """Every open RustDesk session window, with its peer and kind parsed out.

    Hidden windows are skipped, and that is not cosmetic. When a session is
    closed RustDesk tears down the connection and hides the window, but does
    **not** destroy the HWND -- it lingers, still titled, still enumerable, with
    ``IsWindowVisible`` false. Counting those means a closed session looks open
    forever, and worse, ``find_session_window`` could hand the input layer an
    invisible window to click into.
    """
    sessions = []
    for record in enumerate_windows(SESSION_WINDOW_CLASS):
        if not record["title"]:
            continue
        if not include_hidden and not record["visible"]:
            continue
        record.update(parse_session_title(record["title"]))
        sessions.append(record)
    return sessions


def find_session_window(peer=None, kind=None):
    """Find exactly one session window, by peer label/hostname and/or kind."""
    candidates = session_windows()
    if peer:
        wanted = str(peer).lower()
        candidates = [
            window for window in candidates
            if wanted in (window.get("peer_label") or "").lower()
            or wanted in (window.get("hostname") or "").lower()
            or wanted in window["title"].lower()
        ]
    if kind:
        candidates = [window for window in candidates if window.get("kind") == kind]

    if not candidates:
        raise WindowNotFound(
            "No RustDesk session window matches peer=%r kind=%r. Open the session "
            "first -- the bridge drives a window that already exists, it does not "
            "conjure one." % (peer, kind)
        )
    if len(candidates) > 1:
        titles = ", ".join(repr(window["title"]) for window in candidates)
        raise WindowNotFound(
            "%d session windows match: %s. Narrow it with peer or kind."
            % (len(candidates), titles)
        )
    return candidates[0]


def main_window():
    for record in enumerate_windows(MAIN_WINDOW_CLASS):
        if record["visible"]:
            return record
    return None


def restore_and_focus(hwnd):
    """Bring a window to the front, un-minimizing it first if needed.

    Windows only lets the foreground process reassign focus, so this can fail
    silently; the caller must re-read the window state rather than assume.
    """
    SW_RESTORE = 9
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)
    return _window_record(hwnd)


def foreground_window():
    hwnd = _user32.GetForegroundWindow()
    return _window_record(hwnd) if hwnd else None
