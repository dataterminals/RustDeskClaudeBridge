"""RustDeskClaudeBridge -- drive RustDesk by naming a saved peer.

The bridge exists so an agent can work with a remote machine over RustDesk
without ever handling the credential: RustDesk keeps the peer password in its
own encrypted store, and everything here refers to a peer only by its ID, alias
or hostname.

Typical use::

    from rdbridge.ops import Bridge

    bridge = Bridge.from_config()
    print(bridge.status())
    bridge.open("terminal")
"""

__version__ = "0.1.0"

from .errors import (  # noqa: F401
    BridgeError,
    ConfigError,
    PeerError,
    PolicyError,
    RustDeskError,
    RustDeskNotFound,
    RustDeskTimeout,
    SecretRefused,
    WindowNotFound,
)

__all__ = [
    "__version__",
    "BridgeError",
    "ConfigError",
    "PeerError",
    "PolicyError",
    "RustDeskError",
    "RustDeskNotFound",
    "RustDeskTimeout",
    "SecretRefused",
    "WindowNotFound",
]
