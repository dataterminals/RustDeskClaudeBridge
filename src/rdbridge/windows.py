"""Finding RustDesk's windows on this machine.

Verified against RustDesk 1.4.5 on Windows 11:

* the main window is class ``FLUTTER_RUNNER_WIN32_WINDOW``, titled ``RustDesk``
* every session opens its own top-level window of class ``RustdeskMultiWindow``,
  titled ``<alias>@<hostname> - <Session Type> - RustDesk`` -- except a
  terminal, titled ``<alias>@<hostname> - RustDesk`` with no type segment at all

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

_TITLE_SUFFIX = " - RustDesk"

# A terminal window's title has no type segment. In 1.4.5, getWindowName() in
# flutter/lib/common.dart has cases for Main, FileTransfer, ViewCamera,
# PortForward and RemoteDesktop; WindowType.Terminal falls through to the
# default and returns the bare app name. So '<label> - RustDesk' is a terminal.
# The 'terminal' entry above stays in case a later release adds the case.
_UNTYPED_KIND = "terminal"

# Session kinds whose window reports a different kind. --terminal-admin opens
# the same Terminal window type as --terminal, so the titles are identical.
_WINDOW_KINDS = {"terminal-admin": "terminal"}

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


def window_kind(kind):
    """The kind a session window's title reports for session kind ``kind``."""
    return _WINDOW_KINDS.get(kind, kind)


def parse_session_title(title):
    """Split ``<alias>@<host>[ - <Kind>] - RustDesk`` into its parts.

    The label is ``<alias>@<hostname>``, or just the alias (or peer ID) when it
    already contains the hostname or the hostname is unknown -- that is
    ``getDesktopTabLabel`` in common.dart. An alias may itself contain ' - ', so
    the kind segment is found from the right, and a title with none is a
    terminal (see ``_UNTYPED_KIND``).

    A trailing segment that is not a known kind is only read as part of the
    label when an '@' shows where the label ends. Without one it is reported as
    an unknown kind, because calling an unrecognised window a terminal would make
    it a target for typed input.
    """
    if not title.endswith(_TITLE_SUFFIX):
        return {"peer_label": title, "hostname": None, "kind": None,
                "kind_text": None}
    rest = title[:-len(_TITLE_SUFFIX)]
    head, separator, tail = rest.rpartition(" - ")

    if separator and tail.strip().lower() in _TITLE_KINDS:
        who, kind_text = head, tail
        kind = _TITLE_KINDS[tail.strip().lower()]
    elif separator and "@" in rest and " - " not in rest.rpartition("@")[2]:
        # The ' - ' is inside the alias; nothing follows the hostname.
        who, kind_text, kind = rest, None, _UNTYPED_KIND
    elif separator:
        who, kind_text, kind = head, tail, None
    else:
        who, kind_text, kind = rest, None, _UNTYPED_KIND

    alias, at, hostname = who.rpartition("@")
    return {
        "peer_label": alias or who,
        # No '@' means no hostname -- not the whole label in its place.
        "hostname": (hostname or None) if at else None,
        "kind": kind,
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
        wanted_kind = window_kind(kind)
        candidates = [window for window in candidates
                      if window.get("kind") == wanted_kind]

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
