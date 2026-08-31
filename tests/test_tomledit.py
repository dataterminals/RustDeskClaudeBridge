"""Surgical TOML edits, and the guard that keeps the stored password intact."""

import os
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import tomledit  # noqa: E402
from rdbridge.errors import BridgeError  # noqa: E402

# Shaped like a real peers/<id>.toml: the password is a multi-line byte array
# written before any section header, exactly where a naive line-based editor
# would trample it.
SAMPLE = """password = [
    48,
    48,
    108,
]
size = [
    0,
    0,
]
port_forwards = []
disable_audio = true
view_style = 'custom'

[options]
custom-fps = '30'
alias = '''Sylvia's Desktop'''
codec-preference = 'auto'

[info]
username = 'sylvi'
hostname = 'syldesk'
"""


class SetKey(unittest.TestCase):
    def test_replaces_an_existing_top_level_scalar(self):
        out = tomledit.set_key(SAMPLE, "disable_audio", False)
        self.assertFalse(tomllib.loads(out)["disable_audio"])

    def test_replaces_an_existing_empty_array(self):
        out = tomledit.set_key(SAMPLE, "port_forwards", [[2222, "localhost", 22]])
        self.assertEqual(tomllib.loads(out)["port_forwards"],
                         [[2222, "localhost", 22]])

    def test_replaces_a_multiline_array_without_touching_neighbours(self):
        out = tomledit.set_key(SAMPLE, "size", [1, 2, 3, 4])
        parsed = tomllib.loads(out)
        self.assertEqual(parsed["size"], [1, 2, 3, 4])
        self.assertEqual(parsed["password"], [48, 48, 108])

    def test_replaces_a_key_inside_a_section(self):
        out = tomledit.set_key(SAMPLE, "custom-fps", "60", section="options")
        self.assertEqual(tomllib.loads(out)["options"]["custom-fps"], "60")

    def test_inserts_a_new_key_into_an_existing_section(self):
        out = tomledit.set_key(SAMPLE, "brand-new", "yes", section="options")
        self.assertEqual(tomllib.loads(out)["options"]["brand-new"], "yes")

    def test_creates_a_missing_section(self):
        out = tomledit.set_key(SAMPLE, "thing", "1", section="transfer")
        self.assertEqual(tomllib.loads(out)["transfer"]["thing"], "1")

    def test_new_top_level_key_lands_before_the_first_section(self):
        out = tomledit.set_key(SAMPLE, "fresh_key", 7)
        # If it landed after '[options]' it would silently become options.fresh_key.
        self.assertEqual(tomllib.loads(out)["fresh_key"], 7)

    def test_same_named_key_in_a_section_is_not_confused_with_top_level(self):
        text = "custom-fps = 'top'\n\n[options]\ncustom-fps = 'nested'\n"
        out = tomledit.set_key(text, "custom-fps", "changed", section="options")
        parsed = tomllib.loads(out)
        self.assertEqual(parsed["custom-fps"], "top")
        self.assertEqual(parsed["options"]["custom-fps"], "changed")

    def test_quotes_in_values_are_escaped(self):
        out = tomledit.set_key(SAMPLE, "alias", 'He said "hi"', section="options")
        self.assertEqual(tomllib.loads(out)["options"]["alias"], 'He said "hi"')


class FormatValue(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(tomledit.format_value(True), "true")
        self.assertEqual(tomledit.format_value(False), "false")
        self.assertEqual(tomledit.format_value(12), "12")
        self.assertEqual(tomledit.format_value("x"), '"x"')

    def test_nested_arrays(self):
        self.assertEqual(tomledit.format_value([[1, "a", 2]]), '[[1, "a", 2]]')

    def test_none_is_refused(self):
        with self.assertRaises(BridgeError):
            tomledit.format_value(None)


class EditFile(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".toml")
        os.close(handle)
        with open(self.path, "w", encoding="utf-8") as file_handle:
            file_handle.write(SAMPLE)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_edit_preserves_the_password_block_byte_for_byte(self):
        before = tomledit._password_region(SAMPLE)
        tomledit.edit_file(self.path, "port_forwards", [[2222, "localhost", 22]])
        with open(self.path, encoding="utf-8") as handle:
            after = tomledit._password_region(handle.read())
        self.assertIsNotNone(before)
        self.assertEqual(before, after)

    def test_no_op_edit_reports_unchanged(self):
        result = tomledit.edit_file(self.path, "view_style", "custom")
        self.assertFalse(result["changed"])

    def test_result_reports_the_parsed_value(self):
        result = tomledit.edit_file(self.path, "custom-fps", "60", section="options")
        self.assertTrue(result["changed"])
        self.assertEqual(result["value"], "60")

    def test_a_write_that_would_break_parsing_is_refused(self):
        # An unbalanced bracket in a value would corrupt the file; the parse
        # check has to catch it before os.replace runs.
        with open(self.path, encoding="utf-8") as handle:
            original = handle.read()
        with self.assertRaises(BridgeError):
            tomledit.edit_file(self.path, "options", "x")  # collides with [options]
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)


if __name__ == "__main__":
    unittest.main()
