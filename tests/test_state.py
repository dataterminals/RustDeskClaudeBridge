"""Parsing RustDesk's log -- the only channel that reports session state."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import state  # noqa: E402

# Trimmed from a real 1.4.5 log. The shape matters: Rust Debug output, device
# names escaped, and the fields we need scattered through thousands of columns.
PEER_INFO_LINE = (
    '[2026-08-30 18:48:25.287032 -04:00] DEBUG [src\\ui_session_interface.rs:1775] '
    'handle_peer_info :PeerInfo { username: "sylvi", hostname: "syldesk", '
    'platform: "Windows", displays: ['
    'DisplayInfo { x: 1920, y: 0, width: 1920, height: 1080, '
    'name: "\\\\\\\\.\\\\DISPLAY1", online: true, cursor_embedded: false }, '
    'DisplayInfo { x: 0, y: 0, width: 1920, height: 1080, '
    'name: "\\\\\\\\.\\\\DISPLAY2", online: true, cursor_embedded: false }, '
    'DisplayInfo { x: -3840, y: 0, width: 1920, height: 1080, '
    'name: "\\\\\\\\.\\\\DISPLAY3", online: true, cursor_embedded: false }], '
    'current_display: 1, sas_enabled: true, version: "1.4.5", '
    'features: MessageField(Some(Features { privacy_mode: true, terminal: true }))'
)

LOG = "\n".join([
    '[2026-08-30 18:48:20.100000 -04:00] INFO [src\\client.rs:1] Connecting',
    '[2026-08-30 18:48:24.330463 -04:00] INFO [src\\common.rs:1687] Connection secured',
    PEER_INFO_LINE,
    '[2026-08-30 18:48:27.893207 -04:00] INFO [src\\client\\io_loop.rs:1223] Set fps to 30',
    '[2026-08-30 19:10:00.000000 -04:00] INFO [src\\client.rs:9] Connection closed',
    "a line that is not a log record at all",
]) + "\n"


class ParsePeerInfo(unittest.TestCase):
    def setUp(self):
        self.parsed = state.parse_peer_info(PEER_INFO_LINE)

    def test_identity_and_version(self):
        self.assertEqual(self.parsed["hostname"], "syldesk")
        self.assertEqual(self.parsed["username"], "sylvi")
        self.assertEqual(self.parsed["platform"], "Windows")
        self.assertEqual(self.parsed["version"], "1.4.5")

    def test_terminal_feature_is_detected(self):
        # This flag decides whether a real shell is available on the far side,
        # which is the difference between driving pixels and not having to.
        self.assertTrue(self.parsed["features"]["terminal"])
        self.assertTrue(self.parsed["features"]["privacy_mode"])

    def test_every_display_is_found_with_its_offset(self):
        displays = self.parsed["displays"]
        self.assertEqual(len(displays), 3)
        self.assertEqual([d["x"] for d in displays], [1920, 0, -3840])
        self.assertEqual(displays[0]["width"], 1920)
        self.assertEqual([d["index"] for d in displays], [0, 1, 2])

    def test_device_names_are_unescaped(self):
        self.assertEqual(self.parsed["displays"][0]["name"], r"\\.\DISPLAY1")

    def test_current_display_is_read(self):
        self.assertEqual(self.parsed["current_display"], 1)

    def test_missing_fields_are_none_not_an_exception(self):
        parsed = state.parse_peer_info("handle_peer_info :PeerInfo { }")
        self.assertIsNone(parsed["version"])
        self.assertEqual(parsed["displays"], [])


class ReadLog(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".log")
        os.close(handle)
        with open(self.path, "w", encoding="utf-8") as file_handle:
            file_handle.write(LOG)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_events_are_named_and_ordered(self):
        events = state.read_events(self.path)
        self.assertEqual([e["event"] for e in events],
                         ["secured", "peer_info", "fps", "closed"])

    def test_peer_info_body_is_not_dumped_into_the_event(self):
        # The raw line is thousands of characters; the parsed form is available
        # separately and the event list stays readable.
        event = [e for e in state.read_events(self.path) if e["event"] == "peer_info"][0]
        self.assertEqual(event["message"], "(peer info)")

    def test_limit_keeps_the_most_recent(self):
        events = state.read_events(self.path, limit=2)
        self.assertEqual([e["event"] for e in events], ["fps", "closed"])

    def test_latest_peer_info_is_parsed_from_the_file(self):
        info = state.latest_peer_info(self.path)
        self.assertEqual(info["version"], "1.4.5")
        self.assertEqual(len(info["displays"]), 3)
        self.assertIn("time", info)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(state.read_events("no-such.log"), [])
        self.assertIsNone(state.latest_peer_info("no-such.log"))


class DisplaylessSessionsDoNotMaskGeometry(unittest.TestCase):
    """Regression: opening a port forward blinded the pixel layer.

    Every session kind logs a PeerInfo, but only video-bearing ones describe
    displays. A port-forward session logged one with 'displays: []' after a
    remote desktop was already up, and the plain 'latest' entry then reported
    zero displays -- so coordinate mapping refused to work on a session that was
    live and perfectly mappable.
    """

    LATER_WITHOUT_DISPLAYS = (
        '[2026-08-30 20:45:01.825315 -04:00] DEBUG [src\\ui_session_interface.rs:1775] '
        'handle_peer_info :PeerInfo { username: "sylvi", hostname: "syldesk", '
        'platform: "Windows", displays: [], current_display: 0, version: "1.4.5" }'
    )

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".log")
        os.close(handle)
        with open(self.path, "w", encoding="utf-8") as file_handle:
            file_handle.write(LOG + self.LATER_WITHOUT_DISPLAYS + "\n")

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_plain_latest_is_the_displayless_one(self):
        info = state.latest_peer_info(self.path)
        self.assertEqual(info["displays"], [])
        self.assertEqual(info["time"], "2026-08-30 20:45:01.825315 -04:00")

    def test_require_displays_reaches_back_past_it(self):
        info = state.latest_peer_info(self.path, require_displays=True)
        self.assertEqual(len(info["displays"]), 3)
        self.assertEqual(info["time"], "2026-08-30 18:48:25.287032 -04:00")

    def test_require_displays_returns_none_when_none_ever_had_any(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(self.LATER_WITHOUT_DISPLAYS + "\n")
        self.assertIsNone(state.latest_peer_info(self.path, require_displays=True))
        self.assertIsNotNone(state.latest_peer_info(self.path))


if __name__ == "__main__":
    unittest.main()
