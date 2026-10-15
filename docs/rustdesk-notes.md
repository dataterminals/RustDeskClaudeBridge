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
tab. It is the default target for the bridge's `rdb type` and `rustdesk_type`.
Sending keys to it needs no display geometry, since keys go to the focused
window, not to a coordinate. That focus is checked before every key; see
*Keystrokes and focus*.

A small AGPL client against `hbb_common` speaking those messages would give a
true headless shell over RustDesk. That is the endgame, and it is a real
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

The label part comes from `getDesktopTabLabel` in `flutter/lib/common.dart`: the
alias, or the peer ID when there is none, plus `@<hostname>` unless the label
already contains the hostname. So a title need not contain an `@` at all.

**A terminal window has no session-type segment.** It is titled
`Sylvia's Desktop@syldesk - RustDesk`. This was found live on 2026-10-06, with
1.4.5 on both ends, when the bridge failed to recognise its own terminal window.
The source explains it. `getWindowName()` in `common.dart` switches on the window
type, with cases for Main, FileTransfer, ViewCamera, PortForward and
RemoteDesktop. `WindowType.Terminal` has no case, so it falls through to the
default and gets the bare app name. The shipped `data\app.so` agrees: it holds
`Remote Desktop - `, `File Transfer - `, `Port Forward - ` and `View Camera - `
once each, and no `Terminal - `. `master` had the same switch on 2026-10-06. It
would take only a one-line case to change this, so the parser still maps a
`Terminal` segment if one ever appears.

Inside the window, each terminal tab is labelled `<alias> #<n>`, or
`<peer ID> #<n>` without an alias.

More about terminal windows, from the 1.4.5 source:

- **`--terminal-admin` opens the same window type.** It goes through
  `connect(isTerminal: true)` like `--terminal`, so an elevated terminal's title
  is identical to a plain one. The bridge treats the two kinds as one window
  kind, which means `kind="terminal"` also matches an elevated terminal someone
  opened by hand.
- **A second terminal to the same peer may become a tab, not a window.**
  `newTerminal` first asks the existing terminal windows whether one already
  has an active session with that peer, and adds a tab there if so. No new
  window appears, so `open_session` reports `opened: false` while the tab is
  open. Not yet tested live.
- **Switching or closing a tab may retitle the window.** The terminal page's
  `onSelected` calls `getWindowNameWithId` with the *tab key*, which is
  `<peer ID>_<n>` rather than the peer ID. By the source, that leaves the window
  titled `<peer ID>_<n> - RustDesk`, with no alias and no hostname. Not yet seen
  live. If it happens, the parser still calls the window a terminal, but a
  lookup by alias or hostname will not find it. That fails closed with
  `WindowNotFound`.

**A minimized window reports `GetWindowRect` as `-32000,-32000` with a 160×28
size** — not an error. Any geometry derived from it is nonsense.

Every RustDesk window, main or session, has one child of class `FLUTTERVIEW`,
and all of them run on one UI thread: the thread ID is the same for the main
window, a remote desktop and a terminal. Observed on 2026-10-15.

## Keystrokes and focus

**`SendInput` goes to whatever is in the foreground when each key is sent.** It
has no notion of a target window. On 2026-10-15 the bridge typed a
2254-character line, a base64 `-EncodedCommand`, into a terminal on SylDesk. It
checked focus once, then sent the characters one `SendInput` call each, about
10 ms apart: some 23 seconds of typing. The command reported `typed: 2254` and
exited 0. The last ~495 characters were found in a Discord message box on
another monitor. Where the first ~1750 went is unknown. Nothing was sent to
Discord.

The old check had passed, so the terminal *was* in front when typing began. It
was the half-minute after that nobody looked at. So now:

- After asking for the foreground, the bridge polls `GetForegroundWindow()` for
  up to 250 ms and refuses unless the session window is there, whatever
  `SetForegroundWindow` returned. Nothing is sent on a refusal.
- **`GetForegroundWindow()` is checked immediately before every character,
  key, click and scroll.** The first time anything else is in front, input
  stops with `FocusLost` (exit 1). The error says how many went, each verified
  immediately before it was sent; how many were not sent; and the title of the
  window that was in front instead.

The check is per key rather than per 32-character chunk. The keys cannot be
batched (below), so each one is its own `SendInput` call anyway. A check costs
one user32 call, and checking once per chunk would only stretch the unchecked
window from one key to 32. What is left is the moment between a check and the
`SendInput` right after it. A focus change landing exactly there sends one key
to the wrong window, and the next check stops the rest.

**The foreground lock.** Windows lets a process take the foreground only in
narrow cases, such as being the foreground process or having received the last
input event. A background process like the bridge is usually refused while
someone is using the machine; the target's taskbar button flashes instead. The
bridge then refuses too. The usual workaround is to inject an Alt press first
so that Windows counts the caller as having had the last input. It is not used
here, because that Alt press goes to whatever window is in front. That is the
same stray keystroke the check exists to prevent.

**The terminal cannot take a burst.** RustDesk 1.4.5 builds with Flutter
3.24.5 (`FLUTTER_VERSION` in `.github/workflows/flutter-build.yml`) and
`xterm: 4.0.0`. `terminal_page.dart` builds a `TerminalView` with the default
`hardwareKeyboardOnly: false`, so typed text arrives through xterm's
`CustomTextEdit`, a `TextInputClient`. For each update it takes the editing
text minus a placeholder as the insertion, then resets the engine side with
`setEditingState`. That reset is a round trip. Characters that reach the
engine's text model before the reset lands come back in the next update along
with the earlier ones, which get inserted a second time. Nothing in the engine
paces Unicode input either: `KeyboardManager` turns down `VK_PACKET` key-downs
(`if (wparam == VK_PACKET) return false;`), so each `WM_CHAR` goes straight to
the text path. So characters go one per call, paced. This comes from reading
the source, not from a live test.

**`PostMessage(WM_CHAR)` to the window.** A posted message reaches only the
window it was posted to, which is why it is attractive. If it worked without
focus, it would be focus-free. If it needed focus, a focus change would drop
characters instead of sending them to another app. The source says:

- A `WM_CHAR` posted to the `FLUTTERVIEW` child with no key-down before it is
  dispatched by `KeyboardManager` as text. It goes to `OnText` and on to
  `TextInputPlugin::TextHook`, which returns at once when
  `active_model_ == nullptr`, meaning no text field is connected.
- On desktop, Flutter 3.24.5's `FocusManager` follows the app lifecycle
  (`_respondToWindowFocus`). When the app leaves `resumed`, it moves focus to
  the root scope. xterm's `CustomTextEdit` closes its input connection when it
  loses focus. Posted key-downs fare no better, because the terminal's key
  handler hangs off the same focus node.

**Tested live on 2026-10-15**, sylg5 on the far end, with a fresh terminal
opened by `rdb open terminal` at an empty PowerShell prompt. Sylvia authorized
`#rdb` (a comment) with no Enter. Each run posted `#`, `r`, `d`, `b` as
`WM_CHAR` (lParam 1, 30 ms apart) to the terminal's `FLUTTERVIEW`, and the
window was captured with `PrintWindow` before and after:

| Window state when posted | What landed on the remote line |
|---|---|
| Never focused: opened in the background, the Claude app in front throughout | `#rdb` — typed |
| Focused: `SetForegroundWindow` took, foreground verified | `#rdb#rdb` — typed |
| Focused earlier, then lost the foreground; the Claude app in front | `#rdb#rdb` — **dropped** |

The two that typed were echoed back in PSReadLine's comment colour, so the
characters made the full round trip through the remote shell. In the third,
all four `PostMessageW` calls returned success, and a capture a few seconds
later still showed `#rdb#rdb`. The first result contradicts the source
prediction above, and the cursor explains why. xterm draws a filled block while
its view has focus and a hollow one when it does not.

- Right after opening, never focused at OS level: **filled**. The view takes
  focus inside Flutter (`autofocus: true`) when it is built. The engine never
  saw the window deactivate, so the lifecycle never left `resumed`, and the
  input connection stayed open.
- After the window had been focused and then lost the foreground to another
  app: **hollow**. That is the lifecycle suspension the source describes.

So posted `WM_CHAR`s type into a terminal whose view holds Flutter focus.
That holds for one that has never been in front, and for one that is in front
now. They are **silently dropped** by a terminal that was in front and then
lost the foreground, which is the normal state of any terminal someone has
used. `PostMessage` reports success either way. **This is not a focus-free
path.** Relying on "never focused" would mean relying on nobody ever clicking
the window.

What posting does offer is that it cannot misroute: whatever happens, the
characters reach the terminal or nothing. Posting with focus taken and
verified before every character would turn a focus change mid-line into
dropped characters instead of characters in another app. The guard would still
catch the change and report it. It is not adopted yet. Two things are untested:
whether posted characters garble in a burst (the `CustomTextEdit` reset round
trip applies to them too), and whether Enter works as `WM_CHAR 0x0D` or needs
`WM_KEYDOWN VK_RETURN`. Until both are settled, the bridge stays on checked
`SendInput`.

One thing was not checked: whether the dropped characters are really gone or
buffered somewhere. If `#rdb` appears when the window is next focused, it was
buffered. By the source (`TextHook` returns when `active_model_` is null), it
is gone.

**The foreground lock, observed.** The probe ran from a process under the Claude
desktop app. With the Claude app in front, `SetForegroundWindow(terminal)`
took. Handing the foreground back to the Claude app straight afterwards did not
take. A second try, made after Sylvia had clicked into Discord, returned
`FALSE`. So the right to take the foreground seems to come from the Claude app
being in front, and it went once the terminal took over. With another app in
front, the bridge cannot take the foreground, and `require_focus` refuses
rather than typing anywhere.

A truly focus-free path is the headless client under *Remote terminal*.

## The log is the state channel

`handle_peer_info` dumps the whole handshake on connect: remote version,
hostname, username, platform, feature flags, every display's position and size,
`current_display`, supported codecs and resolutions, and `platform_additions`
(`has_file_clipboard`, `is_installed`, privacy-mode implementations).

It is Rust `Debug` output — not a stable format. `state.py` reads a handful of
named fields with regexes and ignores the rest, rather than pretending to parse
it whole.
