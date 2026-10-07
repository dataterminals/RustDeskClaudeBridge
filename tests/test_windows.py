"""Session-window discovery: title parsing, and the hidden-zombie filter."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import policy, sessions, windows  # noqa: E402
from rdbridge.errors import WindowNotFound  # noqa: E402


def _record(title, visible=True, hwnd=1):
    return {"hwnd": hwnd, "pid": 100, "class": windows.SESSION_WINDOW_CLASS,
            "title": title, "visible": visible, "minimized": False, "dpi": 96,
            "window_rect": [0, 0, 800, 600], "client_size": [800, 600],
            "client_origin": [0, 0]}


class TitleParsing(unittest.TestCase):
    def test_alias_host_and_kind(self):
        parsed = windows.parse_session_title(
            "Sylvia's Desktop@syldesk - Remote Desktop - RustDesk")
        self.assertEqual(parsed["peer_label"], "Sylvia's Desktop")
        self.assertEqual(parsed["hostname"], "syldesk")
        self.assertEqual(parsed["kind"], "remote-desktop")

    def test_every_session_kind_maps_back(self):
        for text, kind in (("Remote Desktop", "remote-desktop"),
                           ("File Transfer", "file-transfer"),
                           ("Terminal", "terminal"),
                           ("Port Forward", "port-forward"),
                           ("View Camera", "view-camera")):
            parsed = windows.parse_session_title("a@b - %s - RustDesk" % text)
            self.assertEqual(parsed["kind"], kind, text)

    def test_an_alias_containing_a_dash_survives(self):
        # Split from the right, or 'Sylvia - Desk' would eat the kind field.
        parsed = windows.parse_session_title(
            "Sylvia - Desk@syldesk - Terminal - RustDesk")
        self.assertEqual(parsed["peer_label"], "Sylvia - Desk")
        self.assertEqual(parsed["kind"], "terminal")

    def test_an_unrecognised_title_does_not_raise(self):
        parsed = windows.parse_session_title("something else entirely")
        self.assertIsNone(parsed["kind"])
        self.assertEqual(parsed["peer_label"], "something else entirely")

    def test_unknown_kind_text_is_none_not_a_guess(self):
        parsed = windows.parse_session_title("a@b - Telepathy - RustDesk")
        self.assertIsNone(parsed["kind"])

    def test_the_bare_app_title_is_not_a_session(self):
        self.assertIsNone(windows.parse_session_title("RustDesk")["kind"])


class TerminalTitles(unittest.TestCase):
    """Regression: a terminal session window went unrecognised.

    Found live on 2026-10-06 with RustDesk 1.4.5 on both ends. A terminal
    window is titled '<alias>@<hostname> - RustDesk', with no session-type
    segment, because getWindowName() in common.dart has no Terminal case. The
    parser wanted three segments, so it reported kind None, open_session said
    'opened: false' while the window sat there, and close and input could not
    find it by kind.
    """

    def test_the_real_terminal_title_shape(self):
        parsed = windows.parse_session_title("Sylvia's Desktop@syldesk - RustDesk")
        self.assertEqual(parsed["kind"], "terminal")
        self.assertEqual(parsed["peer_label"], "Sylvia's Desktop")
        self.assertEqual(parsed["hostname"], "syldesk")
        self.assertIsNone(parsed["kind_text"])

    def test_an_alias_containing_a_dash_is_still_a_terminal(self):
        # The '@' marks where the label ends, so the ' - ' belongs to the alias.
        parsed = windows.parse_session_title("Sylvia - Desk@syldesk - RustDesk")
        self.assertEqual(parsed["kind"], "terminal")
        self.assertEqual(parsed["peer_label"], "Sylvia - Desk")
        self.assertEqual(parsed["hostname"], "syldesk")

    def test_a_label_without_a_hostname(self):
        # getDesktopTabLabel drops '@<hostname>' when the alias already
        # contains it.
        parsed = windows.parse_session_title("syldesk - RustDesk")
        self.assertEqual(parsed["kind"], "terminal")
        self.assertEqual(parsed["peer_label"], "syldesk")
        self.assertIsNone(parsed["hostname"])

    def test_an_unknown_segment_without_an_at_is_not_called_a_terminal(self):
        # Ambiguous: alias 'Box - Thing', or an unknown kind 'Thing'. Guessing
        # terminal would make the window a target for typed input.
        parsed = windows.parse_session_title("Box - Thing - RustDesk")
        self.assertIsNone(parsed["kind"])

    def test_a_typed_title_still_wins(self):
        parsed = windows.parse_session_title(
            "Sylvia's Desktop@syldesk - Remote Desktop - RustDesk")
        self.assertEqual(parsed["kind"], "remote-desktop")

    def test_terminal_admin_shares_the_terminal_window_kind(self):
        self.assertEqual(windows.window_kind("terminal-admin"), "terminal")
        self.assertEqual(windows.window_kind("terminal"), "terminal")
        self.assertEqual(windows.window_kind("remote-desktop"), "remote-desktop")


class HiddenWindowsAreSkipped(unittest.TestCase):
    """Regression: a closed session was reported open forever.

    RustDesk hides a session window on close instead of destroying it, so the
    HWND lingers -- still titled, still enumerable. Counting it made close look
    like it had failed, and would have let the input layer target an invisible
    window.
    """

    def setUp(self):
        self._real = windows.enumerate_windows
        windows.enumerate_windows = lambda class_name=None: [
            _record("live@host - Remote Desktop - RustDesk", True, 1),
            _record("dead@host - Port Forward - RustDesk", False, 2),
        ]

    def tearDown(self):
        windows.enumerate_windows = self._real

    def test_only_visible_sessions_are_listed(self):
        listed = windows.session_windows()
        self.assertEqual([w["hwnd"] for w in listed], [1])

    def test_hidden_can_be_asked_for_explicitly(self):
        listed = windows.session_windows(include_hidden=True)
        self.assertEqual(sorted(w["hwnd"] for w in listed), [1, 2])

    def test_a_hidden_window_cannot_be_targeted_for_input(self):
        with self.assertRaises(WindowNotFound):
            windows.find_session_window(kind="port-forward")

    def test_a_visible_window_still_resolves(self):
        found = windows.find_session_window(kind="remote-desktop")
        self.assertEqual(found["hwnd"], 1)

    def test_a_terminal_window_resolves_by_kind_and_peer(self):
        windows.enumerate_windows = lambda class_name=None: [
            _record("Sylvia's Desktop@syldesk - Remote Desktop - RustDesk", True, 1),
            _record("Sylvia's Desktop@syldesk - RustDesk", True, 3),
        ]
        self.assertEqual(windows.find_session_window(kind="terminal")["hwnd"], 3)
        self.assertEqual(
            windows.find_session_window(peer="syldesk", kind="terminal")["hwnd"], 3)
        self.assertEqual(
            windows.find_session_window(kind="terminal-admin")["hwnd"], 3)

    def test_ambiguity_is_refused_rather_than_guessed(self):
        windows.enumerate_windows = lambda class_name=None: [
            _record("a@host - Remote Desktop - RustDesk", True, 1),
            _record("b@host - Remote Desktop - RustDesk", True, 2),
        ]
        with self.assertRaises(WindowNotFound) as caught:
            windows.find_session_window(kind="remote-desktop")
        self.assertIn("2 session windows match", str(caught.exception))


class OpenSessionSeesTheTerminal(unittest.TestCase):
    """open_session reported 'opened: false' for a terminal that had opened."""

    TITLE = "Sylvia's Desktop@syldesk - RustDesk"

    def setUp(self):
        self._real = windows.enumerate_windows
        self.open_windows = []
        windows.enumerate_windows = lambda class_name=None: list(self.open_windows)

    def tearDown(self):
        windows.enumerate_windows = self._real

    def _open(self, kind):
        test = self

        class FakeRustDesk:
            def run(self, args, check=True):
                test.open_windows.append(_record(test.TITLE, True, 7))
                return ""

        peer = SimpleNamespace(id="123456789", label="Sylvia's Desktop")
        return sessions.open_session(FakeRustDesk(), policy.Policy({}), peer,
                                     kind, wait_seconds=1.0)

    def test_terminal_opens(self):
        result = self._open("terminal")
        self.assertTrue(result["opened"])
        self.assertEqual(result["window"]["title"], self.TITLE)

    def test_terminal_admin_opens_the_same_window_type(self):
        self.assertTrue(self._open("terminal-admin")["opened"])


if __name__ == "__main__":
    unittest.main()
