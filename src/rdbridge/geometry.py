"""Mapping remote-screen coordinates into the local session window.

This is the part of the bridge with no clean answer, and it is worth being
plain about why. The remote desktop is a **video stream**. There is no
accessibility tree, no DOM, no element handles -- nothing but pixels and the
window they are painted into. Anything driven at this layer is driven blind.

What the bridge *can* do is the arithmetic. Two things are knowable exactly:

* the remote display's geometry, from the ``PeerInfo`` dump in RustDesk's log
* the session window's client rectangle, from Win32

What is *not* knowable from outside is where inside that client rectangle
RustDesk chose to paint the remote image, which depends on the view style, the
zoom, and the scroll position. So this module offers two mappings and is honest
about which one is in play:

``fit``
    Assumes the remote display is letterboxed to fit the client area, preserving
    aspect ratio. Correct for the adaptive/shrink view styles at scroll origin.
    A guess, and labelled as one.

``calibrated``
    Solved from two known point correspondences the caller supplies after
    looking at a screenshot. Exact for the current view, and stored relative to
    the client origin so that *moving* the window does not invalidate it --
    though resizing or scrolling it does.

Coordinates are stored client-relative for that reason. Screen coordinates are
only produced at the last step.
"""

import json
import os

from .errors import BridgeError, WindowNotFound

# Windows parks minimized windows here; their rects are not real geometry.
_MINIMIZED_SENTINEL = -30000


class Mapping:
    """An affine remote-pixel -> client-pixel transform."""

    def __init__(self, scale_x, scale_y, offset_x, offset_y, source,
                 client_size=None, display_index=None):
        self.scale_x = float(scale_x)
        self.scale_y = float(scale_y)
        self.offset_x = float(offset_x)
        self.offset_y = float(offset_y)
        self.source = source
        self.client_size = list(client_size) if client_size else None
        self.display_index = display_index

    @property
    def exact(self):
        return self.source == "calibrated"

    def to_client(self, x, y):
        return (self.offset_x + self.scale_x * float(x),
                self.offset_y + self.scale_y * float(y))

    def to_remote(self, cx, cy):
        if not self.scale_x or not self.scale_y:
            raise BridgeError("Mapping has a zero scale and cannot be inverted.")
        return ((float(cx) - self.offset_x) / self.scale_x,
                (float(cy) - self.offset_y) / self.scale_y)

    def as_dict(self):
        return {
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "source": self.source,
            "exact": self.exact,
            "client_size": self.client_size,
            "display_index": self.display_index,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["scale_x"], data["scale_y"], data["offset_x"], data["offset_y"],
            data.get("source", "calibrated"), data.get("client_size"),
            data.get("display_index"),
        )


def assert_usable(window):
    """Refuse to compute geometry for a window whose rect is not real."""
    if window.get("minimized") or window["client_origin"][0] <= _MINIMIZED_SENTINEL:
        raise WindowNotFound(
            "The session window '%s' is minimized. Windows reports a minimized "
            "window at -32000,-32000, which would map every click to nowhere. "
            "Restore the window first." % window.get("title", "?")
        )
    width, height = window["client_size"]
    if width <= 0 or height <= 0:
        raise WindowNotFound(
            "The session window has a zero-sized client area (%dx%d)."
            % (width, height)
        )
    return window


def display_by_index(peer_info, index=None):
    """Pick one remote display from a parsed PeerInfo, defaulting to the active one."""
    displays = (peer_info or {}).get("displays") or []
    if not displays:
        raise BridgeError(
            "No remote display geometry is known. It comes from the PeerInfo line "
            "RustDesk logs when a session connects -- open a session first."
        )
    if index is None:
        index = peer_info.get("current_display")
    if index is None:
        index = 0
    for display in displays:
        if display["index"] == index:
            return display
    raise BridgeError(
        "Remote display %s not found; the session reports %d display(s)."
        % (index, len(displays))
    )


def fit_mapping(window, display):
    """Aspect-preserving letterbox fit of one remote display into the client area."""
    assert_usable(window)
    client_w, client_h = window["client_size"]
    scale = min(client_w / float(display["width"]), client_h / float(display["height"]))
    offset_x = (client_w - display["width"] * scale) / 2.0
    offset_y = (client_h - display["height"] * scale) / 2.0
    return Mapping(scale, scale, offset_x, offset_y, "fit",
                   client_size=[client_w, client_h],
                   display_index=display["index"])


def solve_mapping(pairs, window=None, display_index=None):
    """Solve scale and offset from >=2 (remote, client) point correspondences.

    ``pairs`` is [((rx, ry), (cx, cy)), ...] with client-relative targets. Two
    points that differ in both axes are enough; more are averaged pairwise.
    """
    if len(pairs) < 2:
        raise BridgeError("Calibration needs at least two point pairs.")

    scales_x, scales_y = [], []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            (rx1, ry1), (cx1, cy1) = pairs[i]
            (rx2, ry2), (cx2, cy2) = pairs[j]
            if rx2 != rx1:
                scales_x.append((cx2 - cx1) / float(rx2 - rx1))
            if ry2 != ry1:
                scales_y.append((cy2 - cy1) / float(ry2 - ry1))
    if not scales_x or not scales_y:
        raise BridgeError(
            "Calibration points must differ in both axes; got only a horizontal "
            "or only a vertical spread."
        )

    scale_x = sum(scales_x) / len(scales_x)
    scale_y = sum(scales_y) / len(scales_y)
    offset_x = sum(c[0] - scale_x * r[0] for r, c in pairs) / len(pairs)
    offset_y = sum(c[1] - scale_y * r[1] for r, c in pairs) / len(pairs)
    return Mapping(scale_x, scale_y, offset_x, offset_y, "calibrated",
                   client_size=window["client_size"] if window else None,
                   display_index=display_index)


# -- calibration storage ---------------------------------------------------

def calibration_path(repo_root):
    return os.path.join(repo_root, "config", "calibration.json")


def calibration_key(peer_id, kind, display_index):
    return "%s|%s|%s" % (peer_id, kind, display_index)


def load_calibration(path, key):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = data.get(key)
    return Mapping.from_dict(entry) if entry else None


def save_calibration(path, key, mapping):
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data[key] = mapping.as_dict()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    return path


def resolve_mapping(window, display, stored=None):
    """Prefer a stored calibration, but only while it still applies."""
    assert_usable(window)
    if stored is not None:
        if stored.client_size and list(stored.client_size) != list(window["client_size"]):
            stored = None  # the window was resized; the fit changed underneath it
        elif stored.display_index is not None and stored.display_index != display["index"]:
            stored = None
    return stored or fit_mapping(window, display)


def remote_to_screen(window, mapping, x, y):
    """Map a remote pixel to an absolute screen pixel on this machine."""
    assert_usable(window)
    client_x, client_y = mapping.to_client(x, y)
    client_w, client_h = window["client_size"]
    if not (0 <= client_x <= client_w and 0 <= client_y <= client_h):
        raise BridgeError(
            "Remote point (%s, %s) maps to (%.0f, %.0f), outside the %dx%d client "
            "area. The view is probably scrolled or zoomed away from the "
            "calibration -- recalibrate rather than clicking blind."
            % (x, y, client_x, client_y, client_w, client_h)
        )
    origin_x, origin_y = window["client_origin"]
    return int(round(origin_x + client_x)), int(round(origin_y + client_y))
