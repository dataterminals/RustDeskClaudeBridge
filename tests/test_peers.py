"""Peer parsing, and the promise that a password never comes back out."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import peers  # noqa: E402
from rdbridge.errors import PeerError  # noqa: E402

# The password bytes are deliberately values that appear nowhere else in the
# fixture -- 48 would have collided with the digits of the peer ID and made the
# leak assertion below fail for the wrong reason.
PEER_TOML = """password = [
    201,
    202,
    203,
]
port_forwards = [[2222, 'localhost', 22]]
view_style = 'custom'
image_quality = 'balanced'
disable_audio = true
keyboard_mode = 'map'

[options]
alias = '''Sylvia's Desktop'''
custom-fps = '30'
force-always-relay = 'Y'
local_dir = 'C:\\\\Users\\\\sylvi\\\\Downloads'

[info]
username = 'sylvi'
hostname = 'syldesk'
platform = 'Windows'
"""

NO_PASSWORD_TOML = """view_style = 'original'

[options]
alias = 'Laptop'

[info]
hostname = 'sylg5'
platform = 'Windows'
"""


class PeerParsing(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "peers"))
        self._write("900100200.toml", PEER_TOML)
        self._write("111222333.toml", NO_PASSWORD_TOML)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name, text):
        with open(os.path.join(self.root, "peers", name), "w",
                  encoding="utf-8") as handle:
            handle.write(text)

    def test_fields_are_parsed(self):
        peer = peers.resolve(peers.load_peers(self.root), "900100200")
        self.assertEqual(peer.alias, "Sylvia's Desktop")
        self.assertEqual(peer.hostname, "syldesk")
        self.assertEqual(peer.platform, "Windows")
        self.assertTrue(peer.force_relay)
        self.assertEqual(peer.port_forwards, [(2222, "localhost", 22)])
        self.assertEqual(peer.settings["custom-fps"], "30")
        self.assertEqual(peer.settings["keyboard_mode"], "map")

    def test_password_is_reported_but_never_returned(self):
        peer = peers.resolve(peers.load_peers(self.root), "900100200")
        self.assertTrue(peer.has_saved_password)

        # The whole credential-free story rests on this: the flag is true, and
        # no byte of the password appears anywhere in the serialised record.
        record = peer.as_dict()
        self.assertNotIn("password", record)
        self.assertEqual([k for k in record if "password" in k],
                         ["has_saved_password"])

        serialised = json.dumps(record)
        for byte in ("201", "202", "203"):
            self.assertNotIn(byte, serialised)
        self.assertFalse(hasattr(peer, "password"))

    def test_missing_password_is_reported_honestly(self):
        peer = peers.resolve(peers.load_peers(self.root), "111222333")
        self.assertFalse(peer.has_saved_password)

    def test_label_falls_back_from_alias_to_hostname_to_id(self):
        self._write("999.toml", "[info]\nhostname = ''\n")
        loaded = {peer.id: peer for peer in peers.load_peers(self.root)}
        self.assertEqual(loaded["900100200"].label, "Sylvia's Desktop")
        self.assertEqual(loaded["999"].label, "999")


class Resolution(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "peers"))
        for name, text in (("900100200.toml", PEER_TOML),
                           ("111222333.toml", NO_PASSWORD_TOML)):
            with open(os.path.join(self.root, "peers", name), "w",
                      encoding="utf-8") as handle:
                handle.write(text)
        self.peers = peers.load_peers(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_by_id_alias_and_hostname(self):
        self.assertEqual(peers.resolve(self.peers, "900100200").id, "900100200")
        self.assertEqual(peers.resolve(self.peers, "Sylvia's Desktop").id, "900100200")
        self.assertEqual(peers.resolve(self.peers, "syldesk").id, "900100200")

    def test_case_insensitive(self):
        self.assertEqual(peers.resolve(self.peers, "SYLDESK").id, "900100200")

    def test_unique_substring_is_accepted(self):
        self.assertEqual(peers.resolve(self.peers, "syld").id, "900100200")

    def test_ambiguous_substring_is_refused(self):
        # 'syl' matches syldesk and sylg5 -- connecting to the wrong machine is
        # not an acceptable way to resolve a tie.
        with self.assertRaises(PeerError) as caught:
            peers.resolve(self.peers, "syl")
        self.assertIn("matches 2 peers", str(caught.exception))

    def test_unknown_peer_lists_the_known_ones(self):
        with self.assertRaises(PeerError) as caught:
            peers.resolve(self.peers, "nosuchbox")
        self.assertIn("Known peers", str(caught.exception))

    def test_empty_peer_is_refused(self):
        with self.assertRaises(PeerError):
            peers.resolve(self.peers, None)


if __name__ == "__main__":
    unittest.main()
