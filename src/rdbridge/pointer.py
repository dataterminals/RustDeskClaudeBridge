"""Sending real mouse and keyboard input, locally, so RustDesk relays it.

There is no polite version of this. RustDesk forwards whatever input its session
window receives, so the only way to click on the far machine is to move *this*
machine's cursor over the session window and click for real. Consequences the
caller has to accept:

* the local cursor visibly moves, and whatever the person at this keyboard was
  doing is interrupted
* the session window must be focused; input goes wherever focus actually is, not
  where it was supposed to go
* there is no undo, no dry run, and no confirmation from the far side that the
  click landed on what you meant

``SendInput`` is used rather than posting messages to the window, because the
Flutter canvas does not reliably act on synthetic window messages -- and a
half-working click is worse than an honest refusal.

Everything here is gated behind ``policy.check_input`` by its callers in
``ops.py``. Nothing in this module checks policy itself, so do not call it
directly.
"""

import ctypes
import time
from ctypes import wintypes

_user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# Virtual-key codes for keys that have no character to type.
NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "space": 0x20, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78,
    "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send(*inputs):
    array = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise OSError("SendInput delivered %d of %d events" % (sent, len(inputs)))
    return sent


def _absolute(x, y):
    """Normalise a screen point to SendInput's 0..65535 virtual-desktop space."""
    left = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = int(round((int(x) - left) * 65535.0 / max(width - 1, 1)))
    ny = int(round((int(y) - top) * 65535.0 / max(height - 1, 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _mouse(flags, x=None, y=None, data=0):
    dx = dy = 0
    if x is not None and y is not None:
        dx, dy = _absolute(x, y)
        flags |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    return _INPUT(type=INPUT_MOUSE,
                  union=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)))


def cursor_position():
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def move(x, y):
    _send(_mouse(MOUSEEVENTF_MOVE, x, y))
    return cursor_position()


def click(x, y, button="left", double=False, settle=0.08):
    """Move, pause, then click. The pause lets the remote cursor catch up."""
    if button not in _BUTTONS:
        raise ValueError("Unknown mouse button %r" % button)
    down, up = _BUTTONS[button]
    move(x, y)
    time.sleep(settle)
    _send(_mouse(down), _mouse(up))
    if double:
        time.sleep(0.05)
        _send(_mouse(down), _mouse(up))
    return {"x": x, "y": y, "button": button, "double": double}


def scroll(x, y, clicks):
    move(x, y)
    time.sleep(0.05)
    _send(_mouse(MOUSEEVENTF_WHEEL, data=int(clicks) * 120))
    return {"x": x, "y": y, "clicks": clicks}


def type_text(text, delay=0.01):
    """Type a string as Unicode input, so keyboard layout does not matter."""
    for char in str(text):
        code = ord(char)
        down = _INPUT(type=INPUT_KEYBOARD,
                      union=_INPUTUNION(ki=_KEYBDINPUT(0, code,
                                                       KEYEVENTF_UNICODE, 0, None)))
        up = _INPUT(type=INPUT_KEYBOARD,
                    union=_INPUTUNION(ki=_KEYBDINPUT(
                        0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
        _send(down, up)
        if delay:
            time.sleep(delay)
    return {"typed": len(str(text))}


def press(key):
    """Press one named key (enter, tab, escape, f5, arrows...)."""
    name = str(key).strip().lower()
    if name not in NAMED_KEYS:
        raise ValueError(
            "Unknown key %r. Known: %s" % (key, ", ".join(sorted(NAMED_KEYS)))
        )
    code = NAMED_KEYS[name]
    down = _INPUT(type=INPUT_KEYBOARD,
                  union=_INPUTUNION(ki=_KEYBDINPUT(code, 0, 0, 0, None)))
    up = _INPUT(type=INPUT_KEYBOARD,
                union=_INPUTUNION(ki=_KEYBDINPUT(code, 0, KEYEVENTF_KEYUP, 0, None)))
    _send(down, up)
    return {"key": name}
