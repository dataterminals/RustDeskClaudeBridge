"""Reading and writing RustDesk settings.

There is an asymmetry here worth knowing before you use it, and it comes
straight from RustDesk's own source. ``core_main.rs`` gates ``--option`` like
this::

    } else if args[0] == "--option" {
        if is_cli_setting_change_disabled() { ... }
        if crate::platform::is_installed() && is_root() {
            if args.len() == 2 { /* read  */ }
            else if args.len() == 3 { /* write */ }
        } else { println!("Installation and administrative privileges required!"); }

The elevation check wraps **both** branches -- so ``rustdesk --option key`` needs
administrator rights just to *read* a value. Prompting for UAC to answer "what
is the current fps setting" is a poor trade, so:

* **reads** come from RustDesk's own TOML files, which are plain and
  world-readable to the owning user
* **writes** go through elevated ``--option``, which is RustDesk's supported
  path and keeps the running instance's in-memory copy in step

Per-peer settings have no CLI at all and are edited in the peer's TOML file, via
the surgical editor in ``tomledit``.
"""

import os

from . import peers as peers_module
from . import tomledit
from .errors import BridgeError

PEER_TOP_LEVEL = frozenset({
    "view_style", "scroll_style", "image_quality", "custom_image_quality",
    "show_remote_cursor", "follow_remote_cursor", "follow_remote_window",
    "disable_audio", "disable_clipboard", "enable-file-copy-paste",
    "keyboard_mode", "view_only", "privacy_mode", "lock_after_session_end",
    "terminal-persistent", "sync-init-clipboard", "show_quality_monitor",
    "trackpad-speed", "port_forwards",
})


def read_global(config):
    """All non-secret global options, read from RustDesk2.toml (no elevation)."""
    return peers_module.global_options(config["_rustdesk_config_dir"])


def get_global(config, key, policy):
    policy.check_option_read(key)
    options = read_global(config).get("options") or {}
    return options.get(key)


def set_global(config, rd, policy, key, value):
    """Write a global option via elevated ``--option``. Prompts for UAC."""
    policy.check_option_write(key)
    output = rd.run_elevated(["--option", str(key), str(value)])
    return {
        "key": key,
        "value": value,
        "output": output,
        "note": "Written through RustDesk's own --option, so the running "
                "instance sees it without a restart.",
    }


def _peer_path(config, peer_id):
    path = os.path.join(config["_rustdesk_config_dir"], "peers", "%s.toml" % peer_id)
    if not os.path.isfile(path):
        raise BridgeError(
            "No saved config for peer %s at %s. RustDesk writes it after the "
            "first connection." % (peer_id, path)
        )
    return path


def get_peer_setting(config, peer, key):
    """Read one per-peer setting. Secrets are refused by the Peer record itself."""
    from .policy import assert_not_secret
    assert_not_secret(key, "read")
    if key in peer.settings:
        return peer.settings[key]
    return None


def set_peer_setting(config, policy, peer, key, value, force=False):
    """Write one per-peer setting into ``peers/<id>.toml``.

    Refuses while a session window is open, because RustDesk would write its
    in-memory copy back over the edit on close. ``force`` skips that check and
    is a way to lose work.
    """
    policy.check_peer_option_write(key)
    if not force:
        tomledit.assert_safe_to_edit(peer.id)
    section = None if key in PEER_TOP_LEVEL else "options"
    result = tomledit.edit_file(_peer_path(config, peer.id), key, value, section)
    result["peer"] = peer.label
    result["restart_note"] = (
        "RustDesk reads peer config when the session opens; the change applies "
        "to the next session, not a running one."
    )
    return result
