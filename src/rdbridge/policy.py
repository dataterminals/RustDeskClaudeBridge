"""The safety boundary.

Three separate things are gated here, and they fail closed:

1. **Which RustDesk verbs may ever be invoked.** ``FORBIDDEN_ARGS`` is not
   configurable. It exists because RustDesk's CLI mixes harmless queries in with
   verbs that set the permanent password, rewrite the device ID, re-point the
   client at a different rendezvous server, or uninstall the service. A bridge
   that can do those is a bridge that can lock you out of your own machine.

2. **Which configuration keys may be read or written.** Credential-bearing keys
   are refused in *both* directions: ``rustdesk --option key`` happily prints the
   custom-server key to stdout, and stdout ends up in an agent transcript.

3. **What the caller's own config permits** -- session kinds, option writes,
   port forwards, synthetic input. Those live in ``config/bridge.json``.

Deny always beats allow.
"""

import fnmatch
import re

from .errors import PolicyError, SecretRefused

# Verbs the bridge will never pass to RustDesk, whatever the caller asks for.
#
# --password / --set-unlock-pin  : write credentials
# --set-id / --config / --assign : re-identify or re-home this installation
# --deploy                       : same, plus it takes an API token
# --install* / --uninstall* / --remove / --update / --service / --server /
#   --portable-service           : change the installation itself
# --elevate / --run-as-system    : privilege changes the bridge must not make
#                                  implicitly (option writes elevate explicitly)
# --import-config                : replaces the whole config from a file
# --terminal-helper              : RustDesk's own pty child process, not for us
FORBIDDEN_ARGS = frozenset({
    "--password",
    "--set-unlock-pin",
    "--set-id",
    "--config",
    "--assign",
    "--deploy",
    "--install",
    "--silent-install",
    "--install-service",
    "--install-idd",
    "--install-remote-printer",
    "--after-install",
    "--uninstall",
    "--uninstall-service",
    "--uninstall-cert",
    "--uninstall-amyuni-idd",
    "--uninstall-remote-printer",
    "--before-uninstall",
    "--remove",
    "--update",
    "--service",
    "--server",
    "--portable-service",
    "--elevate",
    "--run-as-system",
    "--import-config",
    "--terminal-helper",
    "--noinstall",
    "--quick_support",
})

# Session kinds the bridge understands, mapped to the RustDesk verb that opens
# them. Confirmed against src/core_main.rs and flutter/lib/common.dart in
# rustdesk 1.4.5 -- each takes the peer ID as its next argument.
SESSION_KINDS = {
    "remote-desktop": "--connect",
    "file-transfer": "--file-transfer",
    "terminal": "--terminal",
    "terminal-admin": "--terminal-admin",
    "view-camera": "--view-camera",
    "port-forward": "--port-forward",
    "rdp": "--rdp",
}

# Keys that hold, or help derive, a credential. Matched on word boundaries so
# that 'keyboard_mode' and 'kb_layout_type' are not swept up along with 'key'.
_SECRET_PATTERN = re.compile(
    r"(?:^|[-_])(?:password|passwd|token|secret|pin|salt|privkey|private[-_]?key)(?:[-_]|$)",
    re.IGNORECASE,
)
_SECRET_EXACT = frozenset({
    "key",  # the custom-server key
    "enc_id",
    "hash",
    "trusted_devices",
    "access_token",
    "unlock_pin",
})


def is_secret_key(key):
    """True if ``key`` names something that must never be read out or written."""
    normalized = str(key).strip().lower()
    return normalized in _SECRET_EXACT or bool(_SECRET_PATTERN.search(normalized))


def assert_not_secret(key, action="read"):
    if is_secret_key(key):
        raise SecretRefused(
            "Refusing to %s '%s': it holds a credential. RustDesk keeps peer "
            "passwords and server keys in its own encrypted store, and this "
            "bridge never handles them -- not to read, not to print, not to "
            "just check." % (action, key)
        )


def assert_args_allowed(args):
    """Refuse an argument vector containing any never-allowed verb."""
    for arg in args:
        bare = str(arg).split("=", 1)[0]
        if bare in FORBIDDEN_ARGS:
            raise PolicyError(
                "Refusing to run RustDesk with %s. That verb changes the "
                "installation, its identity, or a stored credential, and is "
                "permanently outside what this bridge does. Run it yourself if "
                "you mean it." % bare
            )
    return list(args)


def _matches_any(value, patterns):
    return any(fnmatch.fnmatchcase(value, str(p)) for p in patterns or ())


class Policy:
    """Reads the caller-configurable half of the boundary from bridge config."""

    def __init__(self, config):
        self._config = config or {}

    def _section(self, name):
        section = self._config.get(name)
        return section if isinstance(section, dict) else {}

    # -- sessions ---------------------------------------------------------
    def check_session(self, kind):
        if kind not in SESSION_KINDS:
            raise PolicyError(
                "Unknown session kind '%s'. Known kinds: %s"
                % (kind, ", ".join(sorted(SESSION_KINDS)))
            )
        sessions = self._section("sessions")
        if not sessions.get("enabled", True):
            raise PolicyError("Opening sessions is disabled in bridge config.")
        if kind in (sessions.get("deny_kinds") or ()):
            raise PolicyError(
                "Session kind '%s' is denied by bridge config (sessions.deny_kinds)."
                % kind
            )
        allowed = sessions.get("allow_kinds")
        if allowed is not None and kind not in allowed:
            raise PolicyError(
                "Session kind '%s' is not in sessions.allow_kinds." % kind
            )
        return SESSION_KINDS[kind]

    # -- options ----------------------------------------------------------
    def check_option_read(self, key):
        assert_not_secret(key, "read")
        if not self._section("options").get("read", True):
            raise PolicyError("Reading global options is disabled in bridge config.")

    def check_option_write(self, key):
        assert_not_secret(key, "write")
        options = self._section("options")
        if not options.get("write", False):
            raise PolicyError(
                "Writing global options is disabled. Set options.write to true in "
                "config/bridge.local.json, and list the key in options.allow_write. "
                "Note that writes run RustDesk elevated and prompt for UAC."
            )
        if _matches_any(key, options.get("deny_write")):
            raise PolicyError("Option '%s' is denied by options.deny_write." % key)
        if not _matches_any(key, options.get("allow_write")):
            raise PolicyError(
                "Option '%s' is not in options.allow_write. Widening that list is a "
                "real decision -- a wrong global option can end remote access to "
                "this machine." % key
            )

    def check_peer_option_write(self, key):
        assert_not_secret(key, "write")
        peer_options = self._section("peer_options")
        if not peer_options.get("write", False):
            raise PolicyError(
                "Writing per-peer settings is disabled. Set peer_options.write to "
                "true in config/bridge.local.json."
            )
        if not _matches_any(key, peer_options.get("allow_write")):
            raise PolicyError(
                "Per-peer setting '%s' is not in peer_options.allow_write." % key
            )

    # -- forwards ---------------------------------------------------------
    def check_forward(self, local_port, remote_host, remote_port):
        forwards = self._section("forwards")
        if not forwards.get("enabled", False):
            raise PolicyError(
                "Port forwarding is disabled. Set forwards.enabled to true and add "
                "the forward to forwards.allow. A forward opens a listener on this "
                "machine that reaches into the remote network."
            )
        wanted = [int(local_port), str(remote_host), int(remote_port)]
        for entry in forwards.get("allow") or ():
            if len(entry) == 3 and [int(entry[0]), str(entry[1]), int(entry[2])] == wanted:
                return tuple(wanted)
        raise PolicyError(
            "Forward %d -> %s:%d is not in forwards.allow."
            % (wanted[0], wanted[1], wanted[2])
        )

    # -- synthetic input --------------------------------------------------
    def check_input(self, keys=False):
        section = self._section("input")
        if not section.get("enabled", False):
            raise PolicyError(
                "Sending input to the remote machine is disabled. Set input.enabled "
                "to true in config/bridge.local.json. There is no undo for a click "
                "on someone else's desktop."
            )
        if keys and not section.get("allow_keys", False):
            raise PolicyError(
                "Sending keystrokes is disabled (input.allow_keys). Mouse input is "
                "governed separately because a stray keystroke goes to whatever "
                "window happens to have focus on the far machine."
            )
        return section

    def explain(self):
        """Summarise the active boundary without touching RustDesk."""
        sessions = self._section("sessions")
        options = self._section("options")
        peer_options = self._section("peer_options")
        forwards = self._section("forwards")
        input_section = self._section("input")
        return {
            "never_allowed_verbs": sorted(FORBIDDEN_ARGS),
            "sessions": {
                "enabled": sessions.get("enabled", True),
                "allow_kinds": sessions.get("allow_kinds"),
                "deny_kinds": sessions.get("deny_kinds") or [],
            },
            "options": {
                "read": options.get("read", True),
                "write": options.get("write", False),
                "allow_write": options.get("allow_write") or [],
                "deny_write": options.get("deny_write") or [],
            },
            "peer_options": {
                "write": peer_options.get("write", False),
                "allow_write": peer_options.get("allow_write") or [],
            },
            "forwards": {
                "enabled": forwards.get("enabled", False),
                "allow": forwards.get("allow") or [],
            },
            "input": {
                "enabled": input_section.get("enabled", False),
                "allow_keys": input_section.get("allow_keys", False),
                "require_focus": input_section.get("require_focus", True),
            },
        }
