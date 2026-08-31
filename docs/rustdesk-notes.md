# RustDesk 1.4.5 notes

Findings behind the bridge's design. Verified against a real install on Windows
11 and against RustDesk's source (AGPL-3.0, `github.com/rustdesk/rustdesk`).
The shipped binary's strings are pooled and mangled — `--terminH1`, `--connecH1`
— so the source is the only reliable reference for the CLI surface.

## The CLI answers on stdout

`RustDesk.exe` is a GUI-subsystem binary, so silence would be the reasonable
expectation. It isn't silent:

```
> RustDesk.exe --version
1.4.5
> RustDesk.exe --get-id
123456789
```

Exit code 0, clean stdout, no window. The entire read-only layer rests on this.

## Session verbs

From `src/core_main.rs` and `flutter/lib/common.dart`:

```
--connect  --play  --file-transfer  --view-camera  --port-forward
--terminal  --terminal-admin  --rdp
```

Each takes the peer ID as its **next** argument. `--password`, `--relay` and
`--switch_uuid` are recognised modifiers (the bridge refuses `--password`).

**These do not start a session in the process you launch.**
`core_main_invoke_new_connection` builds a `rustdesk://<kind>/<id>?<params>` URI
and posts it to the already-running instance with a window message to
`FLUTTER_RUNNER_WIN32_WINDOW_CLASS`. The launched process then exits. So exit
code 0 means *the request was handed over*, not *connected* — session success
has to be observed, which is what the log is for.

`rustdesk://` is also registered as a protocol handler:
`"C:\Program Files\RustDesk\RustDesk.exe" "%1"`.

## Settings: `--option` needs elevation to read

`core_main.rs`:

```rust
} else if args[0] == "--option" {
    if is_cli_setting_change_disabled() { ... }
    if crate::platform::is_installed() && is_root() {
        if args.len() == 2 { /* read  */ }
        else if args.len() == 3 { /* write */ }
    } else { println!("Installation and administrative privileges required!"); }
```

The elevation check wraps **both** branches. Prompting for UAC to answer "what
is the current fps setting" is a bad trade, so the bridge reads from the TOML
files and only elevates for writes.

Refusals arrive as *stdout text*, not a non-zero exit code — `"Settings are
disabled!"` and `"Installation and administrative privileges required!"` both
exit 0. `rustdesk.run_elevated` checks for them explicitly.

## Config layout

`%APPDATA%\RustDesk\config\`

| File | Holds |
|---|---|
| `RustDesk.toml` | device identity, key pair — **never touched** |
| `RustDesk2.toml` | `rendezvous_server`, `nat_type`, `[options]` |
| `RustDesk_local.toml` | UI state, window geometry, **`access_token`** if signed in |
| `RustDesk_ab`, `RustDesk_group` | address book / groups, **encrypted blobs** |
| `peers.toml` | the default template new peers inherit — *not* a peer |
| `peers\<id>.toml` | one saved peer |
| `..\log\RustDesk_rCURRENT.log` | the live log |

A peer file holds the saved password as a multi-line array of bytes, written
**before** any section header. Any whole-file round trip through a TOML
serialiser rewrites it. Hence `tomledit`.

Useful per-peer fields: `alias`, `force-always-relay`, `custom-fps`,
`codec-preference`, `image_quality`, `view_style`, `keyboard_mode`,
`custom_scale_percent`, `local_dir` / `remote_dir` (file-transfer defaults),
`port_forwards`.

## Port forwarding

`PeerConfig` in `hbb_common/src/config.rs`:

```rust
#[serde(default, deserialize_with = "deserialize_vec_i32_string_i32")]
pub port_forwards: Vec<(i32, String, i32)>,
```

`(local_port, remote_host, remote_port)`, persisted per peer. `remote_host` is
resolved **on the remote side**, so a forward reaches the remote LAN, not just
the remote host. That is the most useful thing in RustDesk for an agent.

**The four-argument CLI form is dead on Flutter builds.** The binary still
contains

```
rustdesk --port-forward remote-id listen-port remote-host remote-port
```

but that belongs to the legacy path. `flutter/lib/common.dart` reads
`--port-forward` and takes *only* the next argument as the peer ID; the three
port arguments are dropped. So forwards are written into the peer config, then
the session is opened.

**A seeded forward raises its listener automatically** — confirmed by testing on
2026-08-30. No click in the port-forward window is needed:

```
[src\port_forward.rs:59]  listening on port 0.0.0.0:14445
[src\port_forward.rs:68]  new connection from 127.0.0.1:51259
[src\port_forward.rs:198] new port forwarding connection started
[src\port_forward.rs:80]  connection from 127.0.0.1:51259 closed
```

**The bind address is `0.0.0.0`, not `127.0.0.1`.** The forward is reachable from
the whole local network, and so is whatever it points at on the far side.
Opening a forward is an exposure decision, not just a convenience.

That trace is also the shape of a **live tunnel with a dead target**: the local
socket accepts, the tunnel to the peer establishes ("new port forwarding
connection started"), then it closes with no data because nothing was listening
on the far side. So `listening: true` proves the socket, not the service.

One consequence for anything reading the log: **a port-forward session logs a
`PeerInfo` with `displays: []`**, because it negotiates no video. So does file
transfer. The most recent `PeerInfo` therefore stops describing the screen as
soon as any non-video session opens, even while a remote desktop is still up —
`state.latest_peer_info(..., require_displays=True)` exists for that.

## Remote terminal

1.4.5 has a full remote terminal, gated on the far side being **1.4.1+**. Its
protobuf message set is present in the binary:

```
OpenTerminal  TerminalData  TerminalAction  ResizeTerminal  CloseTerminal
TerminalOpened  TerminalClosed  TerminalError  TerminalResponse
TerminalMessageBox
```

plus a permission flag `enable-terminal`, a `terminal-persistent` per-peer
setting ("keep terminal sessions alive when disconnecting"), and a
`--terminal-admin` variant. The controlled side runs the pty through a helper
over `\\.\pipe\rustdesk_term_in_<id>` / `_out_<id>` (`--terminal-helper`).

Support is advertised in the handshake and lands in the log:

```
version: "1.4.5", features: Features { privacy_mode: true, terminal: true }
```

There is **no headless interface** to it in the shipped binary — it is a Flutter
tab. A small AGPL client against `hbb_common` speaking those messages would give
a true headless shell over RustDesk. That is the endgame, and it is a real
project, not an afternoon.

## Windows

| Class | What |
|---|---|
| `FLUTTER_RUNNER_WIN32_WINDOW` | main window, titled `RustDesk` |
| `RustdeskMultiWindow` | one per session |
| `tray_icon_app` | tray |

Session titles are `<alias>@<hostname> - <Session Type> - RustDesk`, e.g.
`Sylvia's Desktop@syldesk - Remote Desktop - RustDesk`. That title is the only
place a session's peer and kind are exposed externally.

**A minimized window reports `GetWindowRect` as `-32000,-32000` with a 160×28
size** — not an error. Any geometry derived from it is nonsense.

## The log is the state channel

`handle_peer_info` dumps the whole handshake on connect: remote version,
hostname, username, platform, feature flags, every display's position and size,
`current_display`, supported codecs and resolutions, and `platform_additions`
(`has_file_clipboard`, `is_installed`, privacy-mode implementations).

It is Rust `Debug` output — not a stable format. `state.py` reads a handful of
named fields with regexes and ignores the rest, rather than pretending to parse
it whole.
