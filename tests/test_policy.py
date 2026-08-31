"""The safety boundary. These are the tests that matter most in this repo."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import policy  # noqa: E402
from rdbridge.errors import PolicyError, SecretRefused  # noqa: E402


class ForbiddenVerbs(unittest.TestCase):
    def test_credential_writing_verbs_are_refused(self):
        for verb in ("--password", "--set-unlock-pin"):
            with self.assertRaises(PolicyError):
                policy.assert_args_allowed([verb, "hunter2"])

    def test_identity_and_install_verbs_are_refused(self):
        for verb in ("--set-id", "--config", "--assign", "--deploy",
                     "--uninstall", "--install-service", "--import-config",
                     "--elevate", "--run-as-system"):
            with self.assertRaises(PolicyError, msg=verb):
                policy.assert_args_allowed([verb])

    def test_refused_even_when_buried_in_the_vector(self):
        with self.assertRaises(PolicyError):
            policy.assert_args_allowed(["--connect", "123", "--password", "x"])

    def test_key_value_form_is_refused_too(self):
        with self.assertRaises(PolicyError):
            policy.assert_args_allowed(["--password=hunter2"])

    def test_session_and_query_verbs_pass(self):
        for args in (["--version"], ["--get-id"], ["--connect", "123"],
                     ["--terminal", "123"], ["--option", "custom-fps"]):
            self.assertEqual(policy.assert_args_allowed(args), args)


class SecretKeys(unittest.TestCase):
    def test_credential_keys_are_secret(self):
        for key in ("password", "key", "access_token", "unlock_pin", "salt",
                    "enc_id", "trusted_devices", "api-token", "client_secret",
                    "private_key", "hash"):
            self.assertTrue(policy.is_secret_key(key), key)

    def test_lookalike_keys_are_not_secret(self):
        # The reason the matcher uses word boundaries rather than substrings:
        # every one of these contains 'key' or 'pin' and none is a credential.
        for key in ("keyboard_mode", "kb_layout_type", "keep-screen-on",
                    "custom-fps", "codec-preference", "spinner-style",
                    "view_style", "enable-file-copy-paste"):
            self.assertFalse(policy.is_secret_key(key), key)

    def test_reads_are_refused_not_just_writes(self):
        # A read is the leak: --option key prints the value to stdout.
        with self.assertRaises(SecretRefused):
            policy.assert_not_secret("key", "read")


class ConfiguredBoundary(unittest.TestCase):
    def setUp(self):
        self.policy = policy.Policy({
            "sessions": {"enabled": True,
                         "allow_kinds": ["remote-desktop", "terminal"],
                         "deny_kinds": ["rdp"]},
            "options": {"read": True, "write": True,
                        "allow_write": ["custom-fps", "enable-*"],
                        "deny_write": ["enable-terminal"]},
            "peer_options": {"write": True, "allow_write": ["custom-fps"]},
            "forwards": {"enabled": True, "allow": [[2222, "localhost", 22]]},
            "input": {"enabled": False},
        })

    def test_allowed_session_kind_returns_the_verb(self):
        self.assertEqual(self.policy.check_session("terminal"), "--terminal")

    def test_denied_and_unlisted_kinds_are_refused(self):
        with self.assertRaises(PolicyError):
            self.policy.check_session("rdp")
        with self.assertRaises(PolicyError):
            self.policy.check_session("file-transfer")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(PolicyError):
            self.policy.check_session("telepathy")

    def test_option_write_allow_list_supports_globs(self):
        self.policy.check_option_write("custom-fps")
        self.policy.check_option_write("enable-camera")

    def test_deny_beats_allow(self):
        # enable-terminal matches allow ('enable-*') and deny; deny must win.
        with self.assertRaises(PolicyError):
            self.policy.check_option_write("enable-terminal")

    def test_unlisted_option_write_is_refused(self):
        with self.assertRaises(PolicyError):
            self.policy.check_option_write("custom-rendezvous-server")

    def test_forward_must_match_exactly(self):
        self.assertEqual(self.policy.check_forward(2222, "localhost", 22),
                         (2222, "localhost", 22))
        with self.assertRaises(PolicyError):
            self.policy.check_forward(2222, "localhost", 3389)
        with self.assertRaises(PolicyError):
            self.policy.check_forward(9999, "localhost", 22)

    def test_input_is_refused_when_disabled(self):
        with self.assertRaises(PolicyError):
            self.policy.check_input()

    def test_keys_are_gated_separately_from_mouse(self):
        mouse_only = policy.Policy({"input": {"enabled": True, "allow_keys": False}})
        mouse_only.check_input()
        with self.assertRaises(PolicyError):
            mouse_only.check_input(keys=True)


class DefaultsFailClosed(unittest.TestCase):
    """An empty config must permit nothing that reaches out or writes."""

    def setUp(self):
        self.policy = policy.Policy({})

    def test_writes_forwards_and_input_are_off_by_default(self):
        with self.assertRaises(PolicyError):
            self.policy.check_option_write("custom-fps")
        with self.assertRaises(PolicyError):
            self.policy.check_peer_option_write("custom-fps")
        with self.assertRaises(PolicyError):
            self.policy.check_forward(2222, "localhost", 22)
        with self.assertRaises(PolicyError):
            self.policy.check_input()


if __name__ == "__main__":
    unittest.main()
