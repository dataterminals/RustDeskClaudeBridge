"""Key input: where keystrokes go, and what has to be true before they are sent.

Nothing here sends a real event. ``pointer`` and the focus calls are replaced
for every test, so a regression shows up as a failed assertion, not as typing
on this machine.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import cli, mcp_server, ops, pointer, windows  # noqa: E402
from rdbridge.errors import BridgeError, PolicyError  # noqa: E402

TERMINAL = "Sylvia's Desktop@syldesk - RustDesk"
DESKTOP = "Sylvia's Desktop@syldesk - Remote Desktop - RustDesk"


def _record(title, hwnd):
    return {"hwnd": hwnd, "pid": 100, "class": windows.SESSION_WINDOW_CLASS,
            "title": title, "visible": True, "minimized": False, "dpi": 96,
            "window_rect": [0, 0, 800, 600], "client_size": [800, 600],
            "client_origin": [0, 0]}


class _NoGeometry(Exception):
    """Raised if anything asks for remote display geometry."""


class KeyInput(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.focused = []
        self.foreground = None

        def restore_and_focus(hwnd):
            self.focused.append(hwnd)
            self.foreground = hwnd

        def no_geometry(*args, **kwargs):
            raise _NoGeometry("display geometry was requested")

        patches = [
            mock.patch.object(windows, "enumerate_windows",
                              lambda class_name=None: [_record(DESKTOP, 20),
                                                       _record(TERMINAL, 10)]),
            mock.patch.object(windows, "restore_and_focus", restore_and_focus),
            mock.patch.object(windows, "foreground_window",
                              lambda: {"hwnd": self.foreground}
                              if self.foreground else None),
            mock.patch.object(pointer, "type_text",
                              lambda text: self.sent.append(("type", text))
                              or {"typed": len(text)}),
            mock.patch.object(pointer, "press",
                              lambda key: self.sent.append(("press", key))
                              or {"key": key}),
            mock.patch.object(pointer, "click", no_geometry),
            mock.patch.object(pointer, "scroll", no_geometry),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.no_geometry = no_geometry

    def _bridge(self, **input_section):
        settings = {"enabled": True, "allow_keys": True, "require_focus": True}
        settings.update(input_section)
        bridge = ops.Bridge({"rustdesk_exe": "RustDesk.exe", "input": settings})
        bridge.peer = lambda wanted=None: SimpleNamespace(
            id="123456789", alias="Sylvia's Desktop", hostname="syldesk",
            label="Sylvia's Desktop")
        bridge.peer_info = self.no_geometry
        return bridge

    # -- the target ------------------------------------------------------
    def test_typing_into_a_terminal_needs_no_display_geometry(self):
        # Regression: keys went through the pixel mapping, which demands a
        # PeerInfo with displays -- and a terminal session logs none.
        result = self._bridge().remote_type("dir", kind="terminal")
        self.assertEqual(self.sent, [("type", "dir")])
        self.assertEqual(self.focused, [10])
        self.assertEqual(result["window"], TERMINAL)
        self.assertEqual(result["kind"], "terminal")

    def test_press_targets_the_terminal_too(self):
        result = self._bridge().remote_press("enter", kind="terminal")
        self.assertEqual(self.sent, [("press", "enter")])
        self.assertEqual(self.focused, [10])
        self.assertEqual(result["window"], TERMINAL)

    def test_the_default_target_is_still_the_remote_desktop(self):
        self._bridge().remote_type("x")
        self.assertEqual(self.focused, [20])

    def test_a_non_input_window_is_refused(self):
        for kind in ("file-transfer", "port-forward", "view-camera"):
            with self.assertRaises(PolicyError, msg=kind):
                self._bridge().remote_type("x", kind=kind)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.focused, [])

    # -- the policy and focus gates are unchanged -------------------------
    def test_input_disabled_refuses_before_anything(self):
        with self.assertRaises(PolicyError):
            self._bridge(enabled=False).remote_type("x", kind="terminal")
        self.assertEqual((self.sent, self.focused), ([], []))

    def test_keys_disallowed_refuses_before_anything(self):
        with self.assertRaises(PolicyError):
            self._bridge(allow_keys=False).remote_press("enter", kind="terminal")
        self.assertEqual((self.sent, self.focused), ([], []))

    def test_policy_is_checked_before_the_kind(self):
        # A disabled policy is the answer, whatever window was asked for.
        with self.assertRaises(PolicyError) as caught:
            self._bridge(enabled=False).remote_type("x", kind="file-transfer")
        self.assertIn("input.enabled", str(caught.exception))

    def test_failing_to_take_focus_sends_nothing(self):
        bridge = self._bridge()
        with mock.patch.object(windows, "restore_and_focus",
                               lambda hwnd: self.focused.append(hwnd)):
            with self.assertRaises(BridgeError):
                bridge.remote_type("x", kind="terminal")
        self.assertEqual(self.sent, [])

    def test_require_focus_off_skips_the_focus_step(self):
        self._bridge(require_focus=False).remote_type("x", kind="terminal")
        self.assertEqual(self.focused, [])
        self.assertEqual(self.sent, [("type", "x")])

    def test_pointer_input_still_requires_geometry(self):
        with self.assertRaises(_NoGeometry):
            self._bridge().remote_click(10, 10)
        self.assertEqual(self.focused, [])


class _Recorder:
    def __init__(self):
        self.calls = []

    def remote_type(self, text, peer=None, kind="remote-desktop"):
        self.calls.append(("type", text, peer, kind))
        return {}

    def remote_press(self, key, peer=None, kind="remote-desktop"):
        self.calls.append(("press", key, peer, kind))
        return {}


class SurfacesPassTheKind(unittest.TestCase):
    """Regression: cli and mcp_server never passed kind, so keys always went
    to the remote desktop."""

    def _cli(self, *argv):
        recorder = _Recorder()
        fake = SimpleNamespace(from_config=lambda **kwargs: recorder)
        with mock.patch.object(cli, "Bridge", fake):
            cli.run(cli.build_parser().parse_args(list(argv)))
        return recorder.calls

    def test_cli_type_and_press_take_kind(self):
        self.assertEqual(self._cli("type", "--kind", "terminal", "dir"),
                         [("type", "dir", None, "terminal")])
        self.assertEqual(self._cli("press", "enter", "--kind", "terminal"),
                         [("press", "enter", None, "terminal")])

    def test_cli_default_is_unchanged(self):
        self.assertEqual(self._cli("type", "dir"),
                         [("type", "dir", None, "remote-desktop")])

    def test_cli_refuses_a_kind_that_takes_no_keys(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["type", "--kind", "file-transfer", "x"])

    def test_mcp_tools_pass_kind(self):
        recorder = _Recorder()
        tools = mcp_server.TOOLS_BY_NAME
        tools["rustdesk_type"]["handler"](recorder, {"text": "dir",
                                                     "kind": "terminal"})
        tools["rustdesk_press"]["handler"](recorder, {"key": "enter",
                                                      "kind": "terminal"})
        tools["rustdesk_type"]["handler"](recorder, {"text": "dir"})
        self.assertEqual(recorder.calls, [
            ("type", "dir", None, "terminal"),
            ("press", "enter", None, "terminal"),
            ("type", "dir", None, "remote-desktop"),
        ])

    def test_mcp_schemas_offer_only_key_targets(self):
        for name in ("rustdesk_type", "rustdesk_press"):
            tool = mcp_server.TOOLS_BY_NAME[name]
            kind = tool["inputSchema"]["properties"]["kind"]
            self.assertEqual(kind["enum"], list(ops.KEY_TARGET_KINDS), name)
            self.assertIn("kind='terminal'", tool["description"], name)


if __name__ == "__main__":
    unittest.main()
