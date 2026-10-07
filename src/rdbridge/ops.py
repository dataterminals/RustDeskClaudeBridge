"""The Bridge facade -- everything the CLI and the MCP server call.

Layered deliberately, cheapest and safest first:

* **state**    -- peers, settings, session windows, log. Reads files, no UAC,
                  touches nothing.
* **sessions** -- open and close sessions by naming a peer. No credential.
* **settings** -- read from TOML, write through elevated ``--option``.
* **forwards** -- seed a tunnel into the peer config and open it.
* **input**    -- move the real cursor over the session window and click, or
                  type into a focused session window.

Each layer is more likely to surprise you than the one above it, and each is
gated separately in ``config/bridge.json``.
"""

import os

from . import config as config_module
from . import forward as forward_module
from . import geometry
from . import options as options_module
from . import peers as peers_module
from . import pointer
from . import sessions as sessions_module
from . import state
from . import windows
from .errors import BridgeError, PeerError, PolicyError
from .policy import Policy
from .rustdesk import RustDesk

# Session windows that keystrokes may be sent to, preferred target first. In a
# terminal the keys land in a shell the bridge opened; in a remote desktop they
# land in whatever has focus on the far machine. The other windows are local
# RustDesk UI, where typing reaches nothing remote.
KEY_TARGET_KINDS = ("terminal", "remote-desktop")

# The remote desktop has to be asked for. With no terminal open, the default
# fails with WindowNotFound rather than falling back to the riskier target.
DEFAULT_KEY_KIND = "terminal"


class Bridge:
    def __init__(self, config):
        self.config = config
        self.policy = Policy(config)
        self.rd = RustDesk(config["rustdesk_exe"],
                           timeout=config.get("timeout_seconds", 60))

    @classmethod
    def from_config(cls, **kwargs):
        return cls(config_module.load(**kwargs))

    # -- layer 0: state ---------------------------------------------------
    @property
    def _config_dir(self):
        return self.config["_rustdesk_config_dir"]

    @property
    def _log_path(self):
        return state.current_log_path(self.config["_rustdesk_log_dir"])

    def list_peers(self):
        return [peer.as_dict() for peer in peers_module.load_peers(self._config_dir)]

    def peer(self, wanted=None):
        """Resolve a peer by ID, alias or hostname, defaulting to configured."""
        return peers_module.resolve(
            peers_module.load_peers(self._config_dir),
            wanted or self.config.get("peer"),
        )

    def status(self):
        peer_list = peers_module.load_peers(self._config_dir)
        local = peers_module.local_state(self._config_dir)
        info = state.latest_peer_info(self._log_path)
        return {
            "my_id": self.rd.my_id(),
            "version": self.rd.version(),
            "exe": self.config["rustdesk_exe"],
            "config_dir": self._config_dir,
            "peers": len(peer_list),
            "default_peer": self.config.get("peer"),
            "last_peer": local.get("last_peer"),
            "open_sessions": sessions_module.list_sessions(),
            "last_peer_info": info,
        }

    def sessions(self):
        return sessions_module.list_sessions()

    def log_events(self, limit=20):
        return state.read_events(self._log_path, limit=limit)

    def peer_info(self, require_displays=False):
        """The remote side's reported capabilities and display layout.

        Pass ``require_displays`` when the answer is only useful if it describes
        a screen: a port-forward or file-transfer session logs a PeerInfo with
        no displays, which would otherwise mask a live remote desktop's geometry.
        """
        info = state.latest_peer_info(self._log_path,
                                      require_displays=require_displays)
        if info is None:
            if require_displays and state.latest_peer_info(self._log_path):
                raise BridgeError(
                    "The most recent sessions reported no display geometry -- "
                    "port-forward and file-transfer sessions do not negotiate "
                    "video. Open a remote desktop session to learn the remote "
                    "display layout."
                )
            raise BridgeError(
                "No PeerInfo in the log yet. It is written when a session "
                "connects, so open one first."
            )

        # A displayless PeerInfo is the truth about the newest session but a
        # poor answer to "what does the remote look like". Attach the last
        # layout that was actually reported, labelled with when.
        if not require_displays and not info.get("displays"):
            fallback = state.latest_peer_info(self._log_path, require_displays=True)
            if fallback:
                info = dict(info)
                info["displays_last_known"] = {
                    "time": fallback.get("time"),
                    "current_display": fallback.get("current_display"),
                    "displays": fallback["displays"],
                }
                info["note"] = (
                    "The newest session reported no displays -- port-forward and "
                    "file-transfer sessions negotiate no video. The last reported "
                    "layout is under displays_last_known."
                )
        return info

    def global_options(self):
        return options_module.read_global(self.config)

    # -- layer 1: sessions ------------------------------------------------
    def open(self, kind="remote-desktop", peer=None, wait_seconds=20.0):
        return sessions_module.open_session(
            self.rd, self.policy, self.peer(peer), kind, wait_seconds=wait_seconds
        )

    def close(self, peer=None, kind=None):
        return sessions_module.close_session(peer=peer, kind=kind)

    # -- layer 2: settings ------------------------------------------------
    def get_option(self, key):
        return options_module.get_global(self.config, key, self.policy)

    def set_option(self, key, value):
        return options_module.set_global(self.config, self.rd, self.policy, key, value)

    def get_peer_setting(self, key, peer=None):
        return options_module.get_peer_setting(self.config, self.peer(peer), key)

    def set_peer_setting(self, key, value, peer=None, force=False):
        return options_module.set_peer_setting(
            self.config, self.policy, self.peer(peer), key, value, force=force
        )

    # -- layer 3: forwards ------------------------------------------------
    def forwards(self, peer=None):
        return forward_module.list_forwards(self.peer(peer))

    def set_forwards(self, forwards, peer=None, force=False):
        return forward_module.set_forwards(
            self.config, self.policy, self.peer(peer), forwards, force=force
        )

    def open_forward(self, forwards=None, peer=None, wait_seconds=20.0, force=False):
        return forward_module.open_forward(
            self.rd, self.policy, self.peer(peer), forwards=forwards,
            config=self.config, wait_seconds=wait_seconds, force=force,
        )

    # -- layer 4: pixels --------------------------------------------------
    def _calibration_path(self):
        return geometry.calibration_path(self.config["_repo_root"])

    def _session_window(self, peer, kind):
        resolved = self.peer(peer)
        window = windows.find_session_window(peer=resolved.hostname or resolved.alias,
                                             kind=kind)
        return resolved, window

    def _mapping(self, peer=None, kind="remote-desktop", display=None):
        resolved, window = self._session_window(peer, kind)
        geometry.assert_usable(window)
        display_record = geometry.display_by_index(
            self.peer_info(require_displays=True), display
        )
        key = geometry.calibration_key(resolved.id, kind, display_record["index"])
        stored = geometry.load_calibration(self._calibration_path(), key)
        mapping = geometry.resolve_mapping(window, display_record, stored)
        return resolved, window, display_record, mapping, key

    def geometry_report(self, peer=None, kind="remote-desktop", display=None):
        resolved, window, display_record, mapping, _ = self._mapping(peer, kind, display)
        return {
            "peer": resolved.label,
            "window": {
                "title": window["title"],
                "client_size": window["client_size"],
                "client_origin": window["client_origin"],
                "minimized": window["minimized"],
                "dpi": window["dpi"],
            },
            "remote_display": display_record,
            "mapping": mapping.as_dict(),
            "warning": None if mapping.exact else (
                "This mapping is an aspect-preserving guess, not a measurement. "
                "It is right only if the view is fitted to the window and not "
                "scrolled or zoomed. Calibrate before clicking anything that "
                "matters."
            ),
        }

    def calibrate(self, pairs, peer=None, kind="remote-desktop", display=None):
        """Store an exact mapping from >=2 (remote point, screen point) pairs.

        Screen points are converted to client-relative before storing, so the
        calibration survives moving the window.
        """
        resolved, window, display_record, _, key = self._mapping(peer, kind, display)
        origin_x, origin_y = window["client_origin"]
        client_pairs = [
            ((remote[0], remote[1]),
             (screen[0] - origin_x, screen[1] - origin_y))
            for remote, screen in pairs
        ]
        mapping = geometry.solve_mapping(client_pairs, window, display_record["index"])
        geometry.save_calibration(self._calibration_path(), key, mapping)
        return {
            "peer": resolved.label,
            "key": key,
            "mapping": mapping.as_dict(),
            "stored_at": self._calibration_path(),
        }

    def _require_focus(self, settings, window):
        if settings.get("require_focus", True):
            windows.restore_and_focus(window["hwnd"])
            foreground = windows.foreground_window()
            if not foreground or foreground["hwnd"] != window["hwnd"]:
                raise BridgeError(
                    "Could not bring '%s' to the foreground -- Windows only lets "
                    "the active process reassign focus. Input was not sent, "
                    "because it would have gone to whatever is focused instead."
                    % window["title"]
                )

    def _prepare_input(self, peer, kind, display, x, y):
        settings = self.policy.check_input()
        resolved, window, display_record, mapping, _ = self._mapping(peer, kind, display)
        target = geometry.remote_to_screen(window, mapping, x, y)
        self._require_focus(settings, window)
        return resolved, window, mapping, target

    def _prepare_keys(self, peer, kind):
        """Find and focus the window for keystrokes. No pixel mapping is involved.

        Keys go wherever focus is, not to a coordinate, so remote display
        geometry is not required. Requiring it blocked typing into a terminal,
        which negotiates no video and so has no display geometry of its own.
        """
        settings = self.policy.check_input(keys=True)
        if kind not in KEY_TARGET_KINDS:
            raise PolicyError(
                "Keystrokes can only go to a %s session window, not '%s'. Other "
                "session windows are local RustDesk UI."
                % (" or ".join(KEY_TARGET_KINDS), kind)
            )
        resolved, window = self._session_window(peer, kind)
        self._require_focus(settings, window)
        return resolved, window

    def remote_click(self, x, y, button="left", double=False, peer=None,
                     kind="remote-desktop", display=None):
        _, window, mapping, target = self._prepare_input(peer, kind, display, x, y)
        result = pointer.click(target[0], target[1], button=button, double=double)
        result.update({
            "remote": [x, y],
            "screen": list(target),
            "mapping": mapping.source,
            "exact": mapping.exact,
        })
        return result

    def remote_scroll(self, x, y, clicks, peer=None, kind="remote-desktop",
                      display=None):
        _, _, mapping, target = self._prepare_input(peer, kind, display, x, y)
        result = pointer.scroll(target[0], target[1], clicks)
        result.update({"remote": [x, y], "screen": list(target),
                       "mapping": mapping.source})
        return result

    def remote_type(self, text, peer=None, kind=DEFAULT_KEY_KIND):
        _, window = self._prepare_keys(peer, kind)
        result = pointer.type_text(text)
        result.update({"kind": kind, "window": window["title"]})
        return result

    def remote_press(self, key, peer=None, kind=DEFAULT_KEY_KIND):
        _, window = self._prepare_keys(peer, kind)
        result = pointer.press(key)
        result.update({"kind": kind, "window": window["title"]})
        return result

    # -- diagnostics ------------------------------------------------------
    def doctor(self):
        report = {"ok": True, "checks": []}

        def check(name, ok, detail):
            report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
            if not ok:
                report["ok"] = False

        exe = self.config["rustdesk_exe"]
        check("executable", os.path.isfile(exe), exe)
        try:
            check("version", True, self.rd.version())
        except BridgeError as exc:
            check("version", False, str(exc))
        try:
            check("my_id", True, self.rd.my_id())
        except BridgeError as exc:
            check("my_id", False, str(exc))

        check("config_dir", os.path.isdir(self._config_dir), self._config_dir)

        peer_list = peers_module.load_peers(self._config_dir)
        check("saved_peers", bool(peer_list),
              ", ".join(p.label for p in peer_list) or "none saved")

        with_password = [p.label for p in peer_list if p.has_saved_password]
        check("credential_free_connect", bool(with_password),
              ("peers RustDesk can authenticate without any input from the "
               "bridge: %s" % ", ".join(with_password)) if with_password else
              "no peer has a saved password; RustDesk will prompt on connect, "
              "which the bridge cannot and will not answer")

        log_path = self._log_path
        check("log", bool(log_path), log_path or "no log file found")

        info = state.latest_peer_info(log_path) if log_path else None
        if info:
            check("remote_capabilities", True,
                  "%s v%s, terminal=%s, %d display(s)"
                  % (info.get("hostname"), info.get("version"),
                     info["features"].get("terminal"), len(info["displays"])))
        else:
            check("remote_capabilities", True,
                  "no session in the log yet (not an error)")

        try:
            default_peer = self.peer()
            check("default_peer", True, "%s (%s)" % (default_peer.label, default_peer.id))
        except PeerError as exc:
            check("default_peer", False, str(exc))

        report["open_sessions"] = sessions_module.list_sessions()
        report["policy"] = self.policy.explain()
        return report
