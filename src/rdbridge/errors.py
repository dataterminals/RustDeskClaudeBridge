"""Exception types for the bridge.

Every failure path in this package raises one of these, so callers (the CLI and
the MCP server) can turn errors into clean messages instead of tracebacks.
"""


class BridgeError(Exception):
    """Base class for everything this package raises."""


class ConfigError(BridgeError):
    """The bridge configuration is missing, malformed, or incomplete."""


class RustDeskNotFound(ConfigError):
    """The RustDesk executable could not be located."""


class PolicyError(BridgeError):
    """An operation was refused before it was attempted.

    This is the important one: it means nothing was sent to RustDesk, no session
    was opened, and no input reached the remote machine.
    """


class SecretRefused(PolicyError):
    """The operation named a configuration key that holds a credential.

    Raised for reads as well as writes -- printing RustDesk's ``key`` or a stored
    password into a transcript is the leak, not just writing one.
    """


class PeerError(BridgeError):
    """A peer could not be resolved, or its stored config is unreadable."""


class RustDeskError(BridgeError):
    """RustDesk ran but the operation failed."""

    def __init__(self, message, *, exit_code=None, output=None, args=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.output = output
        self.args_used = list(args or [])


class RustDeskTimeout(RustDeskError):
    """RustDesk did not exit within the configured timeout and was killed."""


class WindowNotFound(BridgeError):
    """No RustDesk session window matched, or it is not in a usable state."""
