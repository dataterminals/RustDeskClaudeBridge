"""Port forwarding -- the layer that makes the rest of it unnecessary.

This is the highest-leverage thing RustDesk can do for an agent, and it is easy
to miss because it is buried in a UI tab. A forward is a plain TCP tunnel from a
port on this machine to ``host:port`` *as reachable from the remote machine* --
so it reaches the remote LAN, not just the remote host. Once one exists, working
with the far machine stops being remote-desktop automation and becomes ordinary
local tooling pointed at ``localhost``.

Two findings from RustDesk 1.4.5 shape the implementation.

**The four-argument CLI form does not work on this build.** The string
``rustdesk --port-forward remote-id listen-port remote-host remote-port`` is
still in the binary, but it belongs to the legacy path. In the Flutter build the
argument parser (``flutter/lib/common.dart``) reads ``--port-forward`` and then
takes *only* the next argument as the peer ID; the three port arguments are
dropped on the floor.

**But the forwards are persisted per peer.** ``PeerConfig`` in hbb_common
declares ``port_forwards: Vec<(i32, String, i32)>`` -- ``(local_port,
remote_host, remote_port)`` -- stored in ``peers/<id>.toml``. So the way to set
up a forward without clicking is to write it into the peer config first, then
open the port-forward session.

**Confirmed by testing (2026-08-30, RustDesk 1.4.5):** a seeded forward raises
its listener automatically when the port-forward session opens. No click is
needed. The log says so directly::

    [src\\port_forward.rs:59] listening on port 0.0.0.0:14445
    [src\\port_forward.rs:198] new port forwarding connection started

**And note what that first line says: `0.0.0.0`, not `127.0.0.1`.** RustDesk
binds the forward to *every* interface, so the tunnel is reachable by anything
on the local network, not just by this machine -- and whatever it points at on
the far side is reachable with it. Treat opening a forward as exposing a remote
service to your LAN, because that is what it does. This is the reason
``forwards.enabled`` defaults to false and every forward must be listed
explicitly in ``forwards.allow``.

``verify_listener`` still checks rather than assumes, because "the config was
written" and "a socket is accepting" remain different claims.
"""

import socket

from . import options as options_module
from . import tomledit
from .errors import BridgeError


def list_forwards(peer):
    """The forwards saved for a peer, as [local_port, remote_host, remote_port]."""
    return [list(entry) for entry in peer.port_forwards]


def set_forwards(config, policy, peer, forwards, force=False):
    """Replace the saved forward list for a peer.

    Every entry must be permitted by ``forwards.allow`` in bridge config: a
    forward opens a listening socket on this machine that reaches into a remote
    network, which is not something to allow by accident.
    """
    checked = [list(policy.check_forward(*entry)) for entry in forwards]
    if not force:
        tomledit.assert_safe_to_edit(peer.id)
    path = options_module._peer_path(config, peer.id)
    result = tomledit.edit_file(path, "port_forwards", checked, None)
    result["peer"] = peer.label
    result["forwards"] = checked
    return result


def verify_listener(local_port, timeout=1.0):
    """Is something actually listening on ``local_port`` of this machine?

    The honest test of whether a forward is live. A saved forward that never
    raised a listener looks identical in the config to one that did.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", int(local_port)))
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False
        return True


def open_forward(rd, policy, peer, forwards=None, config=None, wait_seconds=20.0,
                 force=False):
    """Seed the peer's forwards if asked, open the session, then check the port.

    Returns a report that distinguishes 'session opened' from 'port is live',
    because those are genuinely different outcomes.
    """
    from . import sessions

    seeded = None
    if forwards:
        if config is None:
            raise BridgeError("Seeding forwards needs the bridge config.")
        seeded = set_forwards(config, policy, peer, forwards, force=force)

    opened = sessions.open_session(rd, policy, peer, "port-forward",
                                   wait_seconds=wait_seconds)

    wanted = forwards or list_forwards(peer)
    listeners = {}
    for entry in wanted:
        listeners[int(entry[0])] = verify_listener(entry[0])

    return {
        "seeded": seeded,
        "session": opened,
        "listening": listeners,
        "exposure": (
            "RustDesk binds forwards to 0.0.0.0, not 127.0.0.1 -- every "
            "listening port above is reachable from the local network, not only "
            "from this machine. Close the session when you are done with it."
        ),
        "note": (
            "listening=true means a socket is accepting locally; it does not "
            "mean the far-side target is up. A dead target shows as an accepted "
            "connection that closes without data."
        ),
    }
