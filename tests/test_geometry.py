"""Coordinate mapping for the pixel layer, including its refusals."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from rdbridge import geometry  # noqa: E402
from rdbridge.errors import BridgeError, WindowNotFound  # noqa: E402

DISPLAY = {"index": 1, "name": r"\\.\DISPLAY2", "x": 0, "y": 0,
           "width": 1920, "height": 1080}

# A restored window: 1600x900 client area at screen (100, 50).
WINDOW = {"title": "peer - Remote Desktop - RustDesk", "minimized": False,
          "client_size": [1600, 900], "client_origin": [100, 50], "hwnd": 1}

# What Windows reports for a minimized window: not an error, just nonsense.
MINIMIZED = {"title": "peer - Remote Desktop - RustDesk", "minimized": True,
             "client_size": [144, 19], "client_origin": [-31992, -31999],
             "hwnd": 2}


class Usability(unittest.TestCase):
    def test_minimized_windows_are_refused(self):
        with self.assertRaises(WindowNotFound) as caught:
            geometry.assert_usable(MINIMIZED)
        self.assertIn("minimized", str(caught.exception))

    def test_offscreen_origin_is_refused_even_if_the_flag_is_wrong(self):
        sneaky = dict(MINIMIZED, minimized=False)
        with self.assertRaises(WindowNotFound):
            geometry.assert_usable(sneaky)

    def test_zero_sized_client_is_refused(self):
        with self.assertRaises(WindowNotFound):
            geometry.assert_usable(dict(WINDOW, client_size=[0, 0]))

    def test_a_normal_window_passes(self):
        self.assertIs(geometry.assert_usable(WINDOW), WINDOW)


class FitMapping(unittest.TestCase):
    def setUp(self):
        self.mapping = geometry.fit_mapping(WINDOW, DISPLAY)

    def test_scale_is_the_limiting_axis(self):
        # 1600/1920 = 0.8333, 900/1080 = 0.8333 -- same aspect, no letterbox.
        self.assertAlmostEqual(self.mapping.scale_x, 1600 / 1920.0)
        self.assertAlmostEqual(self.mapping.offset_x, 0.0)
        self.assertAlmostEqual(self.mapping.offset_y, 0.0)

    def test_letterboxing_is_centred(self):
        wide = dict(WINDOW, client_size=[1600, 1000])
        mapping = geometry.fit_mapping(wide, DISPLAY)
        self.assertAlmostEqual(mapping.scale_x, 1600 / 1920.0)
        self.assertAlmostEqual(mapping.offset_x, 0.0)
        self.assertAlmostEqual(mapping.offset_y, (1000 - 1080 * (1600 / 1920.0)) / 2)

    def test_a_fit_is_labelled_a_guess(self):
        self.assertEqual(self.mapping.source, "fit")
        self.assertFalse(self.mapping.exact)

    def test_centre_maps_to_centre(self):
        x, y = geometry.remote_to_screen(WINDOW, self.mapping, 960, 540)
        self.assertEqual((x, y), (100 + 800, 50 + 450))

    def test_origin_maps_to_the_client_origin(self):
        self.assertEqual(geometry.remote_to_screen(WINDOW, self.mapping, 0, 0),
                         (100, 50))


class SolveMapping(unittest.TestCase):
    def test_two_points_recover_scale_and_offset(self):
        pairs = [((0, 0), (10, 20)), ((1920, 1080), (10 + 960, 20 + 540))]
        mapping = geometry.solve_mapping(pairs, WINDOW, DISPLAY["index"])
        self.assertAlmostEqual(mapping.scale_x, 0.5)
        self.assertAlmostEqual(mapping.scale_y, 0.5)
        self.assertAlmostEqual(mapping.offset_x, 10)
        self.assertAlmostEqual(mapping.offset_y, 20)
        self.assertTrue(mapping.exact)

    def test_round_trip(self):
        pairs = [((100, 100), (60, 70)), ((1000, 800), (510, 420))]
        mapping = geometry.solve_mapping(pairs, WINDOW, DISPLAY["index"])
        for remote, client in pairs:
            got = mapping.to_client(*remote)
            self.assertAlmostEqual(got[0], client[0], places=6)
            self.assertAlmostEqual(got[1], client[1], places=6)
            back = mapping.to_remote(*client)
            self.assertAlmostEqual(back[0], remote[0], places=6)

    def test_one_point_is_refused(self):
        with self.assertRaises(BridgeError):
            geometry.solve_mapping([((0, 0), (0, 0))])

    def test_collinear_points_are_refused(self):
        # Two points on the same horizontal line cannot fix the vertical scale.
        with self.assertRaises(BridgeError):
            geometry.solve_mapping([((0, 50), (0, 10)), ((100, 50), (50, 10))])


class Bounds(unittest.TestCase):
    def test_a_point_outside_the_client_area_is_refused(self):
        mapping = geometry.fit_mapping(WINDOW, DISPLAY)
        with self.assertRaises(BridgeError) as caught:
            geometry.remote_to_screen(WINDOW, mapping, 5000, 5000)
        self.assertIn("outside", str(caught.exception))


class StoredCalibration(unittest.TestCase):
    def test_a_resize_invalidates_a_stored_mapping(self):
        stored = geometry.Mapping(0.5, 0.5, 0, 0, "calibrated",
                                  client_size=[1600, 900], display_index=1)
        resized = dict(WINDOW, client_size=[1200, 700])
        mapping = geometry.resolve_mapping(resized, DISPLAY, stored)
        self.assertEqual(mapping.source, "fit")

    def test_a_different_display_invalidates_a_stored_mapping(self):
        stored = geometry.Mapping(0.5, 0.5, 0, 0, "calibrated",
                                  client_size=[1600, 900], display_index=3)
        mapping = geometry.resolve_mapping(WINDOW, DISPLAY, stored)
        self.assertEqual(mapping.source, "fit")

    def test_a_matching_stored_mapping_is_kept(self):
        stored = geometry.Mapping(0.5, 0.5, 0, 0, "calibrated",
                                  client_size=[1600, 900], display_index=1)
        mapping = geometry.resolve_mapping(WINDOW, DISPLAY, stored)
        self.assertIs(mapping, stored)

    def test_round_trip_through_a_dict(self):
        stored = geometry.Mapping(0.5, 0.25, 3, 4, "calibrated", [1600, 900], 1)
        restored = geometry.Mapping.from_dict(stored.as_dict())
        self.assertEqual(restored.as_dict(), stored.as_dict())


class DisplaySelection(unittest.TestCase):
    INFO = {"current_display": 1,
            "displays": [dict(DISPLAY, index=0), DISPLAY, dict(DISPLAY, index=2)]}

    def test_defaults_to_the_active_display(self):
        self.assertEqual(geometry.display_by_index(self.INFO)["index"], 1)

    def test_explicit_index(self):
        self.assertEqual(geometry.display_by_index(self.INFO, 2)["index"], 2)

    def test_unknown_index_is_refused(self):
        with self.assertRaises(BridgeError):
            geometry.display_by_index(self.INFO, 9)

    def test_no_displays_explains_why(self):
        with self.assertRaises(BridgeError) as caught:
            geometry.display_by_index({"displays": []})
        self.assertIn("open a session", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
