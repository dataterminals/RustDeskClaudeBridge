"""Session-window discovery: title parsing, and the hidden-zombie filter."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import windows  # noqa: E402
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

    def test_ambiguity_is_refused_rather_than_guessed(self):
        windows.enumerate_windows = lambda class_name=None: [
            _record("a@host - Remote Desktop - RustDesk", True, 1),
            _record("b@host - Remote Desktop - RustDesk", True, 2),
        ]
        with self.assertRaises(WindowNotFound) as caught:
            windows.find_session_window(kind="remote-desktop")
        self.assertIn("2 session windows match", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
