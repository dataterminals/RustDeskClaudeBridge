"""Key input: where keystrokes go, and what has to be true before they are sent.

Nothing here sends a real event. The Win32 calls behind focusing and SendInput
are replaced by ``FakeUser32`` for every test, so a regression shows up as a
failed assertion, not as typing on this machine.

The fake delivers each key to whichever window is in the foreground at the
moment it is sent, as Windows does. That lets a test say not only that typing
stopped, but where every character went -- which is the question that matters
after 2026-10-15, when the tail of a 2254-character line typed "into the
terminal" landed in a Discord message box instead.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import cli, mcp_server, ops, pointer, windows  # noqa: E402
from rdbridge.errors import (BridgeError, FocusLost, PolicyError,  # noqa: E402
                             WindowNotFound)

TERMINAL = "Sylvia's Desktop@syldesk - RustDesk"
DESKTOP = "Sylvia's Desktop@syldesk - Remote Desktop - RustDesk"
CHAT = "#general | Some Server - Discord"

TERMINAL_HWND, DESKTOP_HWND, CHAT_HWND = 10, 20, 99
TITLES = {TERMINAL_HWND: TERMINAL, DESKTOP_HWND: DESKTOP, CHAT_HWND: CHAT}

# Shaped like the line from the incident: a long base64 -EncodedCommand.
LONG_LINE = ("powershell -NoProfile -EncodedCommand "
             + ("SQBuAHYAbwBrAGUALQBXAGUAYgBSAGUAcQB1AGUAcwB0ACAA" * 47))[:2254]


def _record(title, hwnd):
    return {"hwnd": hwnd, "pid": 100, "class": windows.SESSION_WINDOW_CLASS,
            "title": title, "visible": True, "minimized": False, "dpi": 96,
            "window_rect": [0, 0, 800, 600], "client_size": [800, 600],
            "client_origin": [0, 0]}


class FakeUser32:
    """The user32 calls that focus a window and send input, scripted.

    * ``grant_focus`` -- whether SetForegroundWindow actually moves the
      foreground; ``focus_reply`` is what it returns, true or not.
    * ``steal`` -- ``(n, hwnd)``: after the n-th SendInput call, ``hwnd`` takes
      the foreground, as a click on another monitor would.
    * ``revert_after_checks`` -- the foreground goes back to where it was after
      that many GetForegroundWindow calls: a focus change that arrives, is seen,
      and does not stick.
    """

    def __init__(self):
        self.foreground = CHAT_HWND
        self.grant_focus = True
        self.focus_reply = 1
        self.steal = None
        self.revert_after_checks = None
        self.refuse_after = None
        self.focus_requests = []
        self.received = {}
        self.mouse_buttons = []
        self.checks = 0
        self.sends = 0

    # -- focus ----------------------------------------------------------
    def IsIconic(self, hwnd):
        return 0

    def ShowWindow(self, hwnd, command):
        return 1

    def SetForegroundWindow(self, hwnd):
        self.focus_requests.append(hwnd)
        if self.grant_focus:
            self._before_focus, self.foreground = self.foreground, hwnd
        return self.focus_reply

    def GetForegroundWindow(self):
        self.checks += 1
        current = self.foreground
        if self.revert_after_checks is not None and \
                self.checks >= self.revert_after_checks:
            self.foreground = self._before_focus
            self.revert_after_checks = None
        return current

    def GetWindowTextLengthW(self, hwnd):
        return len(TITLES.get(hwnd, ""))

    def GetWindowTextW(self, hwnd, buffer, size):
        buffer.value = TITLES.get(hwnd, "")[:size - 1]
        return len(buffer.value)

    # -- input ----------------------------------------------------------
    def GetSystemMetrics(self, index):
        return 1000

    def GetCursorPos(self, point):
        return 1

    def SendInput(self, count, array, size):
        if self.refuse_after is not None and self.sends >= self.refuse_after:
            return 0
        self.sends += 1
        for item in array[:count]:
            if item.type == pointer.INPUT_MOUSE:
                if item.union.mi.dwFlags & (pointer.MOUSEEVENTF_LEFTDOWN
                                            | pointer.MOUSEEVENTF_RIGHTDOWN):
                    self.mouse_buttons.append(self.foreground)
                continue
            key = item.union.ki
            if key.dwFlags & pointer.KEYEVENTF_KEYUP:
                continue
            inbox = self.received.setdefault(self.foreground, [])
            if key.dwFlags & pointer.KEYEVENTF_UNICODE:
                inbox.append(key.wScan)
            else:
                inbox.append(("vk", key.wVk))
        if self.steal and self.sends == self.steal[0]:
            self.foreground = self.steal[1]
        return count

    # -- what arrived where ---------------------------------------------
    def text(self, hwnd):
        units = [u for u in self.received.get(hwnd, []) if isinstance(u, int)]
        return b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le")

    def keys(self, hwnd):
        return [entry[1] for entry in self.received.get(hwnd, [])
                if isinstance(entry, tuple)]


class _NoGeometry(Exception):
    """Raised if anything asks for remote display geometry."""


class _Win32Faked(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32()

        def no_geometry(*args, **kwargs):
            raise _NoGeometry("display geometry was requested")

        patches = [
            mock.patch.object(windows, "_user32", self.user32),
            mock.patch.object(pointer, "_user32", self.user32),
            mock.patch("time.sleep", lambda seconds: None),
            mock.patch.object(windows, "enumerate_windows",
                              lambda class_name=None: [_record(DESKTOP, DESKTOP_HWND),
                                                       _record(TERMINAL, TERMINAL_HWND)]),
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


class KeyInput(_Win32Faked):
    # -- the target ------------------------------------------------------
    def test_typing_into_a_terminal_needs_no_display_geometry(self):
        # Regression: keys went through the pixel mapping, which demands a
        # PeerInfo with displays -- and a terminal session logs none.
        result = self._bridge().remote_type("dir", kind="terminal")
        self.assertEqual(self.user32.text(TERMINAL_HWND), "dir")
        self.assertEqual(self.user32.focus_requests, [TERMINAL_HWND])
        self.assertEqual(result["window"], TERMINAL)
        self.assertEqual(result["kind"], "terminal")
        self.assertEqual(result["typed"], 3)
        self.assertTrue(result["focus_verified"])

    def test_press_targets_the_terminal_too(self):
        result = self._bridge().remote_press("enter", kind="terminal")
        self.assertEqual(self.user32.keys(TERMINAL_HWND), [pointer.NAMED_KEYS["enter"]])
        self.assertEqual(self.user32.focus_requests, [TERMINAL_HWND])
        self.assertEqual(result["window"], TERMINAL)

    def test_the_default_target_is_the_terminal(self):
        self._bridge().remote_type("x")
        self._bridge().remote_press("enter")
        self.assertEqual(self.user32.focus_requests, [TERMINAL_HWND, TERMINAL_HWND])

    def test_the_remote_desktop_has_to_be_asked_for(self):
        self._bridge().remote_type("x", kind="remote-desktop")
        self.assertEqual(self.user32.focus_requests, [DESKTOP_HWND])
        self.assertEqual(self.user32.text(DESKTOP_HWND), "x")

    def test_no_terminal_does_not_fall_back_to_the_desktop(self):
        with mock.patch.object(windows, "enumerate_windows",
                               lambda class_name=None: [_record(DESKTOP, DESKTOP_HWND)]):
            with self.assertRaises(WindowNotFound):
                self._bridge().remote_type("x")
        self.assertEqual((self.user32.received, self.user32.focus_requests), ({}, []))

    def test_a_non_input_window_is_refused(self):
        for kind in ("file-transfer", "port-forward", "view-camera"):
            with self.assertRaises(PolicyError, msg=kind):
                self._bridge().remote_type("x", kind=kind)
        self.assertEqual(self.user32.received, {})
        self.assertEqual(self.user32.focus_requests, [])

    # -- the policy gates are unchanged ----------------------------------
    def test_input_disabled_refuses_before_anything(self):
        with self.assertRaises(PolicyError):
            self._bridge(enabled=False).remote_type("x", kind="terminal")
        self.assertEqual((self.user32.received, self.user32.focus_requests), ({}, []))

    def test_keys_disallowed_refuses_before_anything(self):
        with self.assertRaises(PolicyError):
            self._bridge(allow_keys=False).remote_press("enter", kind="terminal")
        self.assertEqual((self.user32.received, self.user32.focus_requests), ({}, []))

    def test_policy_is_checked_before_the_kind(self):
        # A disabled policy is the answer, whatever window was asked for.
        with self.assertRaises(PolicyError) as caught:
            self._bridge(enabled=False).remote_type("x", kind="file-transfer")
        self.assertIn("input.enabled", str(caught.exception))

    def test_require_focus_off_skips_the_focus_step_and_says_so(self):
        # Off means exactly this: the keys go to whatever is in front, which
        # here is the chat window. The result must not pretend otherwise.
        result = self._bridge(require_focus=False).remote_type("x", kind="terminal")
        self.assertEqual(self.user32.focus_requests, [])
        self.assertEqual(self.user32.text(CHAT_HWND), "x")
        self.assertFalse(result["focus_verified"])
        self.assertIn("not checked", result["note"])

    def test_pointer_input_still_requires_geometry(self):
        with self.assertRaises(_NoGeometry):
            self._bridge().remote_click(10, 10)
        self.assertEqual(self.user32.focus_requests, [])
        self.assertEqual(self.user32.mouse_buttons, [])


class FocusIsCheckedBeforeEveryKey(_Win32Faked):
    """require_focus has to hold for every key, not just the first.

    Regression for 2026-10-15: focus was checked once, then 2254 characters
    went out one SendInput call at a time over half a minute. The tail went
    to a Discord message box that took the foreground on another monitor, and
    the command still reported ``typed: 2254``.
    """

    # -- focus never acquired -------------------------------------------
    def test_focus_never_acquired_sends_nothing(self):
        self.user32.grant_focus = False
        with self.assertRaises(BridgeError) as caught:
            self._bridge().remote_type(LONG_LINE)
        self.assertEqual(self.user32.received, {})
        message = str(caught.exception)
        self.assertIn(TERMINAL, message)
        self.assertIn(CHAT, message)
        self.assertIn("Nothing was sent", message)

    def test_a_focus_call_that_reports_success_is_not_believed(self):
        # SetForegroundWindow's answer is not evidence; the foreground is.
        self.user32.grant_focus = False
        self.user32.focus_reply = 1
        with self.assertRaises(BridgeError) as caught:
            self._bridge().remote_press("enter")
        self.assertEqual(self.user32.received, {})
        self.assertIn("reported success", str(caught.exception))

    def test_focus_that_arrives_but_does_not_stick_sends_nothing(self):
        # The foreground lock case: the window is in front when first looked
        # at, and gone by the time the first key would be sent.
        self.user32.revert_after_checks = 1
        with self.assertRaises(FocusLost) as caught:
            self._bridge().remote_type("dir")
        self.assertEqual(self.user32.received, {})
        error = caught.exception
        self.assertEqual((error.sent, error.total), (0, 3))
        self.assertEqual(error.foreground, CHAT)
        self.assertIn("nothing was sent", str(error))

    def test_press_checks_focus_right_before_the_key(self):
        self.user32.revert_after_checks = 1
        with self.assertRaises(FocusLost):
            self._bridge().remote_press("enter")
        self.assertEqual(self.user32.received, {})

    # -- focus lost part way --------------------------------------------
    def test_focus_lost_mid_line_stops_there_and_says_how_far_it_got(self):
        self.user32.steal = (1759, CHAT_HWND)
        with self.assertRaises(FocusLost) as caught:
            self._bridge().remote_type(LONG_LINE)
        error = caught.exception
        # Exactly what was confirmed went to the terminal, and not one key
        # reached the window that took the foreground.
        self.assertEqual(self.user32.text(TERMINAL_HWND), LONG_LINE[:1759])
        self.assertNotIn(CHAT_HWND, self.user32.received)
        self.assertEqual((error.sent, error.total), (1759, 2254))
        self.assertEqual((error.window, error.foreground), (TERMINAL, CHAT))
        message = str(error)
        self.assertIn("1759 of 2254 characters", message)
        self.assertIn("495 were not sent", message)
        self.assertIn(CHAT, message)

    def test_focus_lost_is_a_failure_exit(self):
        self.user32.steal = (2, CHAT_HWND)
        with mock.patch.object(cli.Bridge, "from_config",
                               classmethod(lambda cls, **kwargs: self._bridge())), \
                mock.patch("sys.stderr") as stderr, mock.patch("sys.stdout") as stdout:
            code = cli.main(["type", "dir"])
        self.assertEqual(code, 1)
        stdout.write.assert_not_called()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("FocusLost", written)
        self.assertNotIn(CHAT_HWND, self.user32.received)

    def test_a_refused_sendinput_reports_how_far_it_got(self):
        self.user32.refuse_after = 2
        with self.assertRaises(BridgeError) as caught:
            self._bridge().remote_type("dir")
        self.assertIn("after 2 of 3 characters", str(caught.exception))
        self.assertEqual(self.user32.text(TERMINAL_HWND), "di")

    # -- the happy path -------------------------------------------------
    def test_a_long_line_with_focus_held_goes_whole_and_checked(self):
        result = self._bridge().remote_type(LONG_LINE)
        self.assertEqual(result["typed"], 2254)
        self.assertTrue(result["focus_verified"])
        self.assertEqual(self.user32.text(TERMINAL_HWND), LONG_LINE)
        self.assertEqual(list(self.user32.received), [TERMINAL_HWND])
        # Once to acquire, then once before every character.
        self.assertGreaterEqual(self.user32.checks, 1 + len(LONG_LINE))

    def test_a_character_outside_the_bmp_goes_as_a_surrogate_pair(self):
        self._bridge().remote_type("a\U0001F525b")
        self.assertEqual(self.user32.text(TERMINAL_HWND), "a\U0001F525b")
        self.assertEqual(len(self.user32.received[TERMINAL_HWND]), 4)

    def test_a_click_is_held_back_by_the_guard(self):
        def guard(sent):
            raise FocusLost("stop", sent=sent, total=1, window=TERMINAL,
                            foreground=CHAT)

        with self.assertRaises(FocusLost):
            pointer.click(5, 5, guard=guard)
        self.assertEqual(self.user32.mouse_buttons, [])
        pointer.click(5, 5)
        self.assertEqual(self.user32.mouse_buttons, [CHAT_HWND])


class _Recorder:
    """Stands in for Bridge. Its kind default is a sentinel, so a surface that
    stops passing kind shows up as 'unset' instead of hiding behind a default."""

    def __init__(self):
        self.calls = []

    def remote_type(self, text, peer=None, kind="unset"):
        self.calls.append(("type", text, peer, kind))
        return {}

    def remote_press(self, key, peer=None, kind="unset"):
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

    def test_cli_defaults_to_the_terminal(self):
        self.assertEqual(self._cli("type", "dir"),
                         [("type", "dir", None, "terminal")])
        self.assertEqual(self._cli("press", "enter"),
                         [("press", "enter", None, "terminal")])
        self.assertEqual(self._cli("type", "--kind", "remote-desktop", "dir"),
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
        tools["rustdesk_press"]["handler"](recorder, {"key": "enter"})
        tools["rustdesk_type"]["handler"](recorder, {"text": "dir",
                                                     "kind": "remote-desktop"})
        self.assertEqual(recorder.calls, [
            ("type", "dir", None, "terminal"),
            ("press", "enter", None, "terminal"),
            ("type", "dir", None, "terminal"),
            ("press", "enter", None, "terminal"),
            ("type", "dir", None, "remote-desktop"),
        ])

    def test_mcp_schemas_offer_only_key_targets(self):
        for name in ("rustdesk_type", "rustdesk_press"):
            tool = mcp_server.TOOLS_BY_NAME[name]
            kind = tool["inputSchema"]["properties"]["kind"]
            self.assertEqual(kind["enum"], list(ops.KEY_TARGET_KINDS), name)
            self.assertIn("terminal by default", tool["description"], name)


if __name__ == "__main__":
    unittest.main()
