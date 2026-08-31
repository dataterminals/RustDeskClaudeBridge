"""Bridge configuration loading and RustDesk path discovery.

Resolution order, highest priority first:

1. explicit keyword arguments (``--peer`` on the CLI)
2. environment (``RDB_PEER``, ``RDB_EXE``, ``RDB_CONFIG``)
3. ``config/bridge.local.json``  (gitignored, machine-specific)
4. ``config/bridge.json``        (tracked defaults)

Nothing here reads or stores a credential. ``peer`` is a RustDesk ID or a saved
peer alias; RustDesk resolves it to a stored, encrypted password itself.
"""

import copy
import json
import os

from .errors import ConfigError, RustDeskNotFound

DEFAULTS = {
    "peer": None,
    "rustdesk_exe": None,
    "timeout_seconds": 60,
    "sessions": {"enabled": True, "allow_kinds": None, "deny_kinds": []},
    "options": {"read": True, "write": False, "allow_write": [], "deny_write": []},
    "peer_options": {"write": False, "allow_write": []},
    "forwards": {"enabled": False, "allow": []},
    "input": {"enabled": False, "require_focus": True, "allow_keys": False},
}

# Where RustDesk normally lives, per-machine install first.
_EXE_CANDIDATES = (
    r"%ProgramFiles%\RustDesk\RustDesk.exe",
    r"%ProgramFiles(x86)%\RustDesk\RustDesk.exe",
    r"%LOCALAPPDATA%\Programs\RustDesk\RustDesk.exe",
    r"%APPDATA%\RustDesk\RustDesk.exe",
)


def repo_root():
    """Return the repository root (two levels up from this file's package)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def config_dir():
    """RustDesk's own per-user config directory."""
    appdata = os.environ.get("RDB_RUSTDESK_CONFIG") or os.environ.get("APPDATA")
    if not appdata:
        raise ConfigError("APPDATA is not set; cannot locate the RustDesk config.")
    if os.environ.get("RDB_RUSTDESK_CONFIG"):
        return os.path.abspath(appdata)
    return os.path.join(appdata, "RustDesk", "config")


def log_dir():
    """RustDesk's per-user log directory, sibling to the config directory."""
    return os.path.join(os.path.dirname(config_dir()), "log")


def _merge(base, overlay):
    """Shallow-merge per top-level section, so a local file can override one key."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update({k: v for k, v in value.items() if not k.startswith("$")})
            out[key] = merged
        else:
            out[key] = value
    return out


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))


def _from_registry():
    """Ask the rustdesk:// URI handler where the executable is.

    RustDesk registers itself as a protocol handler at install time, so this
    finds relocated installs that the fixed candidate list would miss.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            r"rustdesk\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None
    command = str(command).strip()
    if command.startswith('"'):
        candidate = command[1:].split('"', 1)[0]
    else:
        candidate = command.split(" ", 1)[0]
    return candidate or None


def find_rustdesk_exe(explicit=None):
    """Locate ``RustDesk.exe``.

    Despite being a GUI-subsystem binary it writes to an attached console, so
    ``--version`` and ``--get-id`` are genuinely readable from a pipe. That is
    what makes a CLI-driven bridge possible at all.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("RDB_EXE"):
        candidates.append(os.environ["RDB_EXE"])
    candidates.extend(os.path.expandvars(c) for c in _EXE_CANDIDATES)
    from_registry = _from_registry()
    if from_registry:
        candidates.append(from_registry)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)

    raise RustDeskNotFound(
        "Could not find RustDesk.exe. Install RustDesk, or set rustdesk_exe in "
        "config/bridge.local.json (or the RDB_EXE environment variable) to its "
        "full path. Looked in:\n  " + "\n  ".join(str(c) for c in candidates)
    )


def load(config_path=None, peer=None, rustdesk_exe=None, require_exe=True):
    """Load and validate bridge configuration into a plain dict."""
    root = repo_root()
    base_path = config_path or os.environ.get("RDB_CONFIG") or os.path.join(
        root, "config", "bridge.json"
    )

    data = _read_json(base_path)
    if data is None:
        raise ConfigError(
            "Bridge config not found at %s. Copy config/bridge.json from the "
            "repository, or pass --config." % base_path
        )

    config = _merge(DEFAULTS, data)

    local = _read_json(os.path.join(root, "config", "bridge.local.json"))
    if local:
        config = _merge(config, local)

    if os.environ.get("RDB_PEER"):
        config["peer"] = os.environ["RDB_PEER"]
    if peer:
        config["peer"] = peer

    if require_exe:
        config["rustdesk_exe"] = find_rustdesk_exe(
            rustdesk_exe or config.get("rustdesk_exe")
        )
    config["_config_path"] = base_path
    config["_repo_root"] = root
    config["_rustdesk_config_dir"] = config_dir()
    config["_rustdesk_log_dir"] = log_dir()
    return config
