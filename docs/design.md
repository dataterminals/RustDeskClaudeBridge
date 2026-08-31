# Design

## The problem

RustDesk is built for a person at a keyboard. Everything an agent would want —
"is it connected", "send this file", "change that setting" — is expressed as a
window with a button in it. The question is which parts of that can be reached
without pixels, and how honest the parts that can't should be about it.

The answer turned out better than expected, for one structural reason: RustDesk
already holds the credential. That removes the problem that killed SFTP
automation here, and it is the same shape as the WinSCP bridge — *name the thing,
let the tool resolve the secret*.

## Layers

Ordered by blast radius. Each is separately gated, and everything that writes or
reaches out is off by default.

### 0 — State (free, read-only)

`peers.py`, `state.py`, `windows.py`.

Peers and settings come from RustDesk's TOML. Session windows come from Win32.
Live session state comes from parsing the log, which is the only channel
RustDesk offers — there is no status API.

The one design rule: **never return a credential**. Peer records strip
`password`/`hash`/`salt`/`enc_id` and report only `has_saved_password`.
`RustDesk_local.toml` is filtered for `access_token`.

### 1 — Sessions (contacts another machine)

`sessions.py`. `RustDesk.exe --<verb> <peer-id>`, with no credential.

Because the verb only hands a URI to the running instance, a zero exit code
proves nothing. `open_session` snapshots the open windows, launches, then waits
for a *new* matching window and reports whether one appeared. When none does it
says so, and lists what is open, instead of claiming success.

### 2 — Settings (prompts for UAC)

`options.py`, `tomledit.py`.

Split, because RustDesk's own gate is asymmetric in an inconvenient way: reads
would elevate too. So reads come from the TOML files, and only writes go through
`--option`, which is RustDesk's supported path and keeps the running instance's
in-memory copy in step.

Per-peer settings have no CLI at all, so they are edited in the file. That is
where `tomledit` earns its existence: the saved password is a multi-line byte
array, and re-serialising the file would rewrite it. The editor replaces exactly
one key's value text and then proves it behaved — the result must still parse,
and the password assignment must be byte-identical, or the write is abandoned.

The other trap is ordering: RustDesk keeps config in memory and writes it back
on close, so an edit made under a live session is silently lost.
`assert_safe_to_edit` refuses rather than letting that happen quietly.

### 3 — Forwards (opens a network path)

`forward.py`. **The highest-leverage layer, and the one most worth reaching for.**

A forward is a TCP tunnel from a local port to `host:port` as resolved on the
remote side. Once one exists, the far machine stops being a remote desktop and
becomes a local port — reachable by any ordinary tool. It converts an automation
problem into a normal one.

The obvious payoff is SSH, and it is worth being clear that this only works if
something is already listening over there. A machine with no SSH server gets
nothing from a forward to port 22; the tunnel will come up and carry no traffic.
Where there is no service to reach, **the remote terminal is the shell**, not the
forward — see layer 1.

Because the Flutter build drops the positional port arguments, the forward is
written into the peer's `port_forwards` first and the session opened after.
Testing settled the question that was open here: **the listener comes up on its
own**, no click required.

It also surfaced the thing worth knowing before enabling any of this — RustDesk
binds forwards to `0.0.0.0`. The tunnel is reachable from the whole local
network, and so is whatever it points at on the far side. That is an exposure
decision, which is why it defaults off and requires each forward to be named.

`open_forward` still reports session-opened and port-listening as **separate**
facts, because a live tunnel to a dead target accepts connections and returns
nothing — indistinguishable from a working one until you send bytes.

### 4 — Pixels (no undo)

`geometry.py`, `pointer.py`.

The remote desktop is a video stream. There is no accessibility tree, no DOM, no
element handles — nothing to query about what is on screen. Anything driven here
is driven blind, and no amount of engineering changes that.

What *is* knowable exactly is the arithmetic on both ends: the remote display
geometry (from the log) and the session window's client rectangle (from Win32).
What is not knowable from outside is where inside that rectangle RustDesk chose
to paint, which depends on view style, zoom and scroll. So the bridge offers two
mappings and never blurs them:

- **`fit`** — aspect-preserving letterbox. Correct for a fitted, unscrolled view.
  A guess, and reported as one.
- **`calibrated`** — solved from point correspondences measured off a screenshot.
  Exact for the current view. Stored *client-relative*, so moving the window
  keeps it valid; resizing or scrolling invalidates it, and that is detected.

Input is `SendInput` against the real cursor, because the Flutter canvas does not
act reliably on posted window messages — and a half-working click is worse than
an honest refusal. Consequences are documented rather than smoothed over: the
local cursor visibly moves, focus must be taken, and mouse and keyboard are
gated separately because a stray keystroke lands wherever focus actually is.

## The safety boundary

Two halves.

**Not configurable.** `policy.FORBIDDEN_ARGS` blocks the verbs that write
credentials (`--password`, `--set-unlock-pin`), re-identify or re-home the
installation (`--set-id`, `--config`, `--assign`, `--deploy`), or change the
install itself. A bridge that can run those is a bridge that can lock you out of
your own machine. There is no flag to reach them.

Credential-bearing keys are refused for reads as well as writes, because
`--option key` prints to stdout and stdout becomes a transcript. The matcher uses
word boundaries so `keyboard_mode` and `kb_layout_type` are not swept up with
`key` — a test pins both directions.

**Configurable.** Session kinds, option writes, forwards and input, in
`config/bridge.json`. Deny beats allow. Defaults fail closed, and a test asserts
that an empty config permits nothing that writes or reaches out.

## Settled by testing

**Does a seeded forward raise its listener without a click?** Yes. Writing
`port_forwards` into the peer config and opening the port-forward session is
enough; the log reports `listening on port 0.0.0.0:14445` and the tunnel
establishes on first connection. The `0.0.0.0` half of that was not expected and
is now documented everywhere it matters.

**A displayless PeerInfo masks the screen.** Found by running it: every session
kind logs a `PeerInfo`, but only video-bearing ones populate `displays`. Opening
a port forward wrote one with `displays: []`, and the pixel layer — which had
been reading simply "the latest" — went blind while a remote desktop was still
live and perfectly mappable. Fixed with `require_displays`, and pinned by a
regression test.

## Open questions

1. **A headless protocol client.** The terminal protobufs are all present and
   RustDesk is AGPL-3.0, so a small Rust client against `hbb_common` could open a
   Terminal session and pipe stdin/stdout — a real headless shell over RustDesk,
   no GUI at all. It would make layer 4 almost unnecessary, and would matter most
   where there is no SSH server to tunnel to. Anything distributed from it
   inherits AGPL.
2. **Driving the terminal without a protocol client.** Short of (1), the remote
   terminal is a Flutter tab, so reading its output means pixels or clipboard.
   Whether RustDesk's clipboard sync is a usable output channel is untested.
3. **File transfer.** Currently only "open the window". Whether the transfer UI
   can be driven usefully, or whether a forward plus a real file protocol is
   always the better answer, is unexplored.
