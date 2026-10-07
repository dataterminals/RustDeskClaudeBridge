"""Command line interface.

Usage lives in ``--help``. Exit codes are the machine-readable part:

* 0   success
* 1   any BridgeError (policy refusal, RustDesk failure, unresolved peer)
* 2   usage error
* 130 interrupted

Output is pretty-printed JSON throughout, so the same commands are useful from a
script as from a prompt.
"""

import argparse
import json
import sys

from .errors import BridgeError
from .ops import KEY_TARGET_KINDS, Bridge


def _print(value):
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str, sort_keys=False))


def _coerce(text):
    """Turn a CLI string into the TOML-appropriate Python value."""
    lowered = str(text).strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _parse_forward(text):
    """Parse ``LOCAL:HOST:REMOTE`` (e.g. 2222:localhost:22)."""
    parts = str(text).split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "Forward must be LOCAL:HOST:REMOTE, e.g. 2222:localhost:22"
        )
    try:
        return [int(parts[0]), parts[1], int(parts[2])]
    except ValueError:
        raise argparse.ArgumentTypeError("Ports must be integers: %s" % text)


def _parse_point(text):
    parts = str(text).split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Point must be X,Y")
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        raise argparse.ArgumentTypeError("Point must be two integers: %s" % text)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rdb",
        description="Drive RustDesk by naming a saved peer. Never handles a "
                    "credential.",
    )
    parser.add_argument("--peer", help="Peer ID, alias or hostname")
    parser.add_argument("--config", help="Path to bridge.json")
    parser.add_argument("--exe", help="Path to RustDesk.exe")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="RustDesk state at a glance")
    sub.add_parser("peers", help="Saved peers (never their passwords)")
    sub.add_parser("doctor", help="Check the whole setup end to end")
    sub.add_parser("policy", help="Show the active safety boundary; no side effects")
    sub.add_parser("sessions", help="Open session windows")
    sub.add_parser("info", help="Remote capabilities and display layout")
    sub.add_parser("options", help="Global RustDesk options (read from TOML)")

    log_parser = sub.add_parser("log", help="Recent notable log events")
    log_parser.add_argument("-n", "--limit", type=int, default=20)

    open_parser = sub.add_parser("open", help="Open a session with a peer")
    open_parser.add_argument(
        "kind", nargs="?", default="remote-desktop",
        choices=["remote-desktop", "file-transfer", "terminal", "view-camera",
                 "port-forward", "terminal-admin", "rdp"],
    )
    open_parser.add_argument("--wait", type=float, default=20.0)

    close_parser = sub.add_parser("close", help="Close a session window")
    close_parser.add_argument("--kind")

    option_parser = sub.add_parser("option", help="Get or set a global option")
    option_parser.add_argument("key")
    option_parser.add_argument("value", nargs="?",
                               help="Omit to read. Writing prompts for UAC.")

    setting_parser = sub.add_parser("setting", help="Get or set a per-peer setting")
    setting_parser.add_argument("key")
    setting_parser.add_argument("value", nargs="?", help="Omit to read")
    setting_parser.add_argument("--force", action="store_true",
                                help="Edit even with a session open (risks being "
                                     "overwritten)")

    forwards_parser = sub.add_parser("forwards", help="List saved port forwards")
    forwards_parser.add_argument("--set", type=_parse_forward, nargs="+",
                                 metavar="LOCAL:HOST:REMOTE")
    forwards_parser.add_argument("--open", action="store_true",
                                 help="Open the port-forward session afterwards")
    forwards_parser.add_argument("--force", action="store_true")

    geometry_parser = sub.add_parser("geometry", help="How remote pixels map to this screen")
    geometry_parser.add_argument("--kind", default="remote-desktop")
    geometry_parser.add_argument("--display", type=int)

    calibrate_parser = sub.add_parser(
        "calibrate",
        help="Store an exact mapping from remote/screen point pairs",
    )
    calibrate_parser.add_argument("--pair", nargs=2, action="append", required=True,
                                  metavar=("REMOTE_X,Y", "SCREEN_X,Y"))
    calibrate_parser.add_argument("--kind", default="remote-desktop")
    calibrate_parser.add_argument("--display", type=int)

    click_parser = sub.add_parser("click", help="Click at a remote coordinate")
    click_parser.add_argument("point", type=_parse_point, metavar="X,Y")
    click_parser.add_argument("--button", default="left",
                              choices=["left", "right", "middle"])
    click_parser.add_argument("--double", action="store_true")
    click_parser.add_argument("--display", type=int)

    scroll_parser = sub.add_parser("scroll", help="Scroll at a remote coordinate")
    scroll_parser.add_argument("point", type=_parse_point, metavar="X,Y")
    scroll_parser.add_argument("clicks", type=int)
    scroll_parser.add_argument("--display", type=int)

    key_kind_help = ("Session window to send keys to. Prefer 'terminal': keys land "
                     "in the remote shell. With 'remote-desktop' they go to "
                     "whatever has focus on the far desktop.")

    type_parser = sub.add_parser("type", help="Type text into a remote session")
    type_parser.add_argument("text")
    type_parser.add_argument("--kind", default="remote-desktop",
                             choices=KEY_TARGET_KINDS, help=key_kind_help)

    press_parser = sub.add_parser("press", help="Press one named key remotely")
    press_parser.add_argument("key")
    press_parser.add_argument("--kind", default="remote-desktop",
                              choices=KEY_TARGET_KINDS, help=key_kind_help)

    return parser


def run(args):
    bridge = Bridge.from_config(peer=args.peer, config_path=args.config,
                                rustdesk_exe=args.exe)
    command = args.command

    if command == "status":
        return bridge.status()
    if command == "peers":
        return bridge.list_peers()
    if command == "doctor":
        return bridge.doctor()
    if command == "policy":
        return bridge.policy.explain()
    if command == "sessions":
        return bridge.sessions()
    if command == "info":
        return bridge.peer_info()
    if command == "options":
        return bridge.global_options()
    if command == "log":
        return bridge.log_events(limit=args.limit)
    if command == "open":
        return bridge.open(args.kind, wait_seconds=args.wait)
    if command == "close":
        return bridge.close(peer=args.peer, kind=args.kind)
    if command == "option":
        if args.value is None:
            return {args.key: bridge.get_option(args.key)}
        return bridge.set_option(args.key, args.value)
    if command == "setting":
        if args.value is None:
            return {args.key: bridge.get_peer_setting(args.key)}
        return bridge.set_peer_setting(args.key, _coerce(args.value),
                                       force=args.force)
    if command == "forwards":
        if args.set and args.open:
            return bridge.open_forward(forwards=args.set, force=args.force)
        if args.set:
            return bridge.set_forwards(args.set, force=args.force)
        if args.open:
            return bridge.open_forward(force=args.force)
        return bridge.forwards()
    if command == "geometry":
        return bridge.geometry_report(kind=args.kind, display=args.display)
    if command == "calibrate":
        pairs = [(_parse_point(remote), _parse_point(screen))
                 for remote, screen in args.pair]
        return bridge.calibrate(pairs, kind=args.kind, display=args.display)
    if command == "click":
        return bridge.remote_click(args.point[0], args.point[1],
                                   button=args.button, double=args.double,
                                   display=args.display)
    if command == "scroll":
        return bridge.remote_scroll(args.point[0], args.point[1], args.clicks,
                                    display=args.display)
    if command == "type":
        return bridge.remote_type(args.text, kind=args.kind)
    if command == "press":
        return bridge.remote_press(args.key, kind=args.kind)

    raise BridgeError("Unhandled command: %s" % command)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _print(run(args))
        return 0
    except BridgeError as exc:
        sys.stderr.write("%s: %s\n" % (type(exc).__name__, exc))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130
