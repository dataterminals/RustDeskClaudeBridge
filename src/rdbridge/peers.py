"""Reading RustDesk's saved peers.

Each peer RustDesk has connected to gets a file at
``%APPDATA%/RustDesk/config/peers/<id>.toml``, holding its alias, the last-seen
hostname and platform, the per-session preferences, and -- the reason this
module is careful -- the saved password as an encrypted byte array.

**The password field is dropped on read and never returned.** It is the whole
point of the design: because RustDesk already holds an encrypted credential for
a saved peer, the bridge can open a session by naming the peer and no secret
ever passes through this code. Reading it out would throw that away for nothing.

``peers.toml`` in the config root is *not* a peer. It is the default template
new peers inherit, and it is skipped here.
"""

import os
import tomllib

from .errors import PeerError

# Dropped from every record this module returns. 'password' is the encrypted
# blob; 'hash' and 'salt' would help attack it.
_SECRET_FIELDS = frozenset({"password", "hash", "salt", "enc_id", "access_token"})

# Per-peer preferences worth surfacing. The full file has far more, most of it
# window geometry; this is the part a caller might reasonably want to read.
_INTERESTING = (
    "view_style",
    "scroll_style",
    "image_quality",
    "custom_image_quality",
    "keyboard_mode",
    "disable_audio",
    "disable_clipboard",
    "enable-file-copy-paste",
    "show_remote_cursor",
    "follow_remote_cursor",
    "view_only",
    "privacy_mode",
    "lock_after_session_end",
    "terminal-persistent",
    "direct_failures",
)


class Peer:
    """One saved RustDesk peer. Never carries a credential.

    ``has_saved_password`` is passed in rather than derived from ``data``,
    because ``data`` has already had the password stripped out by the time it
    gets here -- deriving it would silently always be False, and the whole
    credential-free connect story rests on that flag being right.
    """

    def __init__(self, peer_id, data, has_saved_password=False):
        self.id = peer_id
        options = data.get("options") or {}
        info = data.get("info") or {}
        self.alias = options.get("alias") or ""
        self.hostname = info.get("hostname") or ""
        self.username = info.get("username") or ""
        self.platform = info.get("platform") or ""
        self.force_relay = options.get("force-always-relay") == "Y"
        self.port_forwards = [
            tuple(entry) for entry in (data.get("port_forwards") or [])
            if isinstance(entry, (list, tuple)) and len(entry) == 3
        ]
        self.settings = {
            key: data[key] for key in _INTERESTING if key in data
        }
        self.settings.update({
            key: value for key, value in options.items()
            if key in ("custom-fps", "codec-preference", "collapse_toolbar",
                       "local_dir", "custom_scale_percent")
        })
        self.has_saved_password = bool(has_saved_password)

    @property
    def label(self):
        return self.alias or self.hostname or self.id

    def as_dict(self):
        return {
            "id": self.id,
            "alias": self.alias,
            "hostname": self.hostname,
            "username": self.username,
            "platform": self.platform,
            "force_relay": self.force_relay,
            "port_forwards": [list(f) for f in self.port_forwards],
            "settings": dict(self.settings),
            # Reported, never read: this is what makes a credential-free
            # connect possible, so a caller needs to know it is there.
            "has_saved_password": self.has_saved_password,
        }

    def __repr__(self):
        return "<Peer %s %r>" % (self.id, self.label)


def _load_toml(path):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _strip_secrets(data):
    return {k: v for k, v in data.items() if k not in _SECRET_FIELDS}


def load_peers(config_dir):
    """Load every saved peer, newest file first. Unreadable files are skipped."""
    peers_dir = os.path.join(config_dir, "peers")
    if not os.path.isdir(peers_dir):
        return []

    found = []
    for name in os.listdir(peers_dir):
        if not name.lower().endswith(".toml"):
            continue
        path = os.path.join(peers_dir, name)
        try:
            data = _load_toml(path)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        peer = Peer(
            os.path.splitext(name)[0],
            _strip_secrets(data),
            has_saved_password=bool(data.get("password")),
        )
        found.append((os.path.getmtime(path), peer))

    found.sort(key=lambda pair: pair[0], reverse=True)
    return [peer for _, peer in found]


def local_state(config_dir):
    """Read the non-secret parts of RustDesk_local.toml.

    That file also holds ``access_token`` for a signed-in RustDesk account, so
    it is filtered rather than returned whole.
    """
    path = os.path.join(config_dir, "RustDesk_local.toml")
    try:
        data = _load_toml(path)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    options = data.get("options") or {}
    # peer-sorting is stored under [ui_flutter], not [options].
    ui_flutter = data.get("ui_flutter") or {}
    return {
        "last_peer": data.get("remote_id") or "",
        "favorites": list(data.get("fav") or []),
        "theme": options.get("theme"),
        "peer_sorting": ui_flutter.get("peer-sorting"),
    }


def global_options(config_dir):
    """Read the non-secret parts of RustDesk2.toml."""
    path = os.path.join(config_dir, "RustDesk2.toml")
    try:
        data = _load_toml(path)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    from .policy import is_secret_key
    options = {
        key: value for key, value in (data.get("options") or {}).items()
        if not is_secret_key(key)
    }
    return {
        "rendezvous_server": data.get("rendezvous_server"),
        "nat_type": data.get("nat_type"),
        "options": options,
    }


def resolve(peer_list, wanted):
    """Find one peer by ID, alias or hostname.

    Exact matches win. A substring is accepted only when it identifies exactly
    one peer -- connecting to the wrong machine because 'desk' matched two of
    them is not a failure mode worth having.
    """
    if not wanted:
        raise PeerError(
            "No peer given. Set 'peer' in config/bridge.local.json or pass --peer."
        )
    wanted = str(wanted).strip()
    lowered = wanted.lower()

    for peer in peer_list:
        if peer.id == wanted:
            return peer
    for peer in peer_list:
        if peer.alias.lower() == lowered or peer.hostname.lower() == lowered:
            return peer

    partial = [
        peer for peer in peer_list
        if lowered in peer.alias.lower() or lowered in peer.hostname.lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise PeerError(
            "'%s' matches %d peers: %s. Use the full alias or the ID."
            % (wanted, len(partial), ", ".join(p.label for p in partial))
        )

    known = ", ".join("%s (%s)" % (p.label, p.id) for p in peer_list) or "none saved"
    raise PeerError("No saved peer matches '%s'. Known peers: %s" % (wanted, known))
