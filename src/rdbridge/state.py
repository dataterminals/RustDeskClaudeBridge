"""Session state, read out of RustDesk's own log.

RustDesk exposes no status API, but it logs the whole session handshake to
``%APPDATA%/RustDesk/log/RustDesk_rCURRENT.log`` -- including a full ``PeerInfo``
dump with the remote version, its feature flags, and the geometry of every
remote display. That is the only place the bridge can learn what it needs for
the pixel layer, and the only way to tell whether a session actually connected
after a session verb returned 0.

The parsers here are deliberately narrow. The log prints Rust ``Debug`` output,
which is not a stable format, so this reads a handful of named fields with
regexes and ignores everything else rather than pretending to parse it whole.
"""

import os
import re

_LINE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<level>[A-Z]+)\s+\[(?P<src>[^\]]+)\]\s+(?P<msg>.*)$"
)
_DISPLAY = re.compile(
    r'DisplayInfo \{ x: (-?\d+), y: (-?\d+), width: (\d+), height: (\d+), '
    r'name: "([^"]*)"'
)
_VERSION = re.compile(r'version: "([^"]+)"')
_CURRENT_DISPLAY = re.compile(r"current_display: (\d+)")
_TERMINAL = re.compile(r"terminal: (true|false)")
_PRIVACY = re.compile(r"privacy_mode: (true|false)")
_HOSTNAME = re.compile(r'hostname: "([^"]*)"')
_USERNAME = re.compile(r'username: "([^"]*)"')
_PLATFORM = re.compile(r'platform: "([^"]*)"')

# Substrings worth reporting, mapped to a stable event name. Everything else in
# the log is noise for our purposes.
_EVENTS = (
    ("handle_peer_info", "peer_info"),
    ("Connection secured", "secured"),
    ("Set fps to", "fps"),
    ("Connection closed", "closed"),
    ("connection loop exited", "closed"),
    ("Failed to connect", "connect_failed"),
    ("Failed to establish", "connect_failed"),
    ("Wrong password", "auth_failed"),
    ("Connection opened from", "incoming"),
)

# The log can grow to megabytes across a long uptime; only the tail is useful.
_TAIL_BYTES = 1024 * 1024


def current_log_path(log_dir):
    """The active log file, or the most recently written one."""
    current = os.path.join(log_dir, "RustDesk_rCURRENT.log")
    if os.path.isfile(current):
        return current
    if not os.path.isdir(log_dir):
        return None
    candidates = [
        os.path.join(log_dir, name)
        for name in os.listdir(log_dir)
        if name.lower().endswith(".log")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _read_tail(path, max_bytes=_TAIL_BYTES):
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # discard the partial line
        return handle.read().decode("utf-8", errors="replace")


def _unescape(name):
    """Undo the Rust Debug escaping on a device name such as \\\\.\\DISPLAY1."""
    try:
        return name.encode("utf-8", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return name


def parse_peer_info(message):
    """Pull the fields we use out of one ``handle_peer_info`` line."""
    displays = []
    for index, match in enumerate(_DISPLAY.finditer(message)):
        x, y, width, height, name = match.groups()
        displays.append({
            "index": index,
            "name": _unescape(name),
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
        })

    version = _VERSION.search(message)
    current = _CURRENT_DISPLAY.search(message)
    terminal = _TERMINAL.search(message)
    privacy = _PRIVACY.search(message)
    hostname = _HOSTNAME.search(message)
    username = _USERNAME.search(message)
    platform = _PLATFORM.search(message)

    return {
        "version": version.group(1) if version else None,
        "hostname": hostname.group(1) if hostname else None,
        "username": username.group(1) if username else None,
        "platform": platform.group(1) if platform else None,
        "current_display": int(current.group(1)) if current else None,
        "displays": displays,
        "features": {
            "terminal": terminal.group(1) == "true" if terminal else None,
            "privacy_mode": privacy.group(1) == "true" if privacy else None,
        },
    }


def read_events(log_path, limit=40):
    """Return the most recent notable log events, oldest first."""
    if not log_path or not os.path.isfile(log_path):
        return []
    events = []
    for line in _read_tail(log_path).splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        message = match.group("msg")
        for needle, name in _EVENTS:
            if needle in message:
                events.append({
                    "time": match.group("ts"),
                    "level": match.group("level"),
                    "source": match.group("src"),
                    "event": name,
                    # peer_info lines are thousands of characters of Debug dump;
                    # the parsed form is available separately.
                    "message": message if name != "peer_info" else "(peer info)",
                })
                break
    return events[-limit:]


def latest_peer_info(log_path, require_displays=False):
    """Parse the most recent ``handle_peer_info`` line, or None.

    ``require_displays`` picks the most recent entry that actually carries
    display geometry. This is not a nicety: **every session kind logs a
    PeerInfo, but only the video-bearing ones describe displays.** Opening a
    port-forward or file-transfer session writes a PeerInfo with an empty
    display list, so the plain "latest" entry stops describing the screen the
    moment any other session is opened -- while a remote desktop is still up and
    still perfectly mappable. The pixel layer asks for displays specifically.
    """
    if not log_path or not os.path.isfile(log_path):
        return None
    latest = None
    latest_with_displays = None
    for line in _read_tail(log_path).splitlines():
        if "handle_peer_info" not in line:
            continue
        match = _LINE.match(line)
        if not match:
            continue
        parsed = parse_peer_info(match.group("msg"))
        parsed["time"] = match.group("ts")
        latest = parsed
        if parsed["displays"]:
            latest_with_displays = parsed
    return latest_with_displays if require_displays else latest
