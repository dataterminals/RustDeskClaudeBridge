# RustDeskClaudeBridge

A compatibility layer that lets Claude (or any agent, or you at a terminal) work
with a remote machine **through a RustDesk peer you have already saved**, without
anyone ever handling the password.

Built for driving `syldesk` from `sylg5` and back, but nothing here is specific
to those two machines.

## Why this exists

The obvious way to give an agent remote access is to hand it the credential.
That is the one thing this repo refuses to do — the same line held in
[`WinSCPClaudeBridge`](../WinSCPClaudeBridge).

It works because RustDesk has already solved it. A peer you have connected to
before has its password stored, encrypted, in RustDesk's own config. So the
bridge names a peer and RustDesk authenticates from its own store:

```
RustDesk.exe --connect 123456789
```

No password is read, decoded, passed, logged or "just checked". RustDesk's CLI
*does* accept `--password`, and `policy.FORBIDDEN_ARGS` makes it permanently
unreachable from here, along with `--set-id`, `--config`, `--assign`, and every
install/uninstall verb. Those are not configurable, and a test pins them.

## The four layers

Ordered by how much they can surprise you. Each is gated separately in
`config/bridge.json`, and everything that writes or reaches out is **off by
default**.

| Layer | What it does | Cost |
|---|---|---|
| **0. State** | Peers, settings, open sessions, live state from RustDesk's log | Free, read-only |
| **1. Sessions** | Open/close remote desktop, file transfer, **terminal**, port forward | Contacts another machine |
| **2. Settings** | Read from TOML; write through elevated `--option` | Prompts for UAC |
| **3. Forwards** | Tunnel a TCP port from the remote network to `localhost` | Opens a network path |
| **4. Pixels** | Focus the session window and click for real | No undo |

**Reach for layer 3 before layer 4.** A port forward turns remote work into
ordinary local tooling pointed at `localhost`, and it is reliable in a way that
clicking on a video stream never will be. If the far side is RustDesk 1.4.1 or
newer, `open terminal` gives a real shell and is usually better still.

**Layer 4 is honest about being blind.** The remote desktop is a video stream —
no accessibility tree, no DOM, no element handles. The bridge does the
arithmetic (remote display geometry from the log, window geometry from Win32)
and tells you whether the mapping is a calibrated measurement or a guess. It
cannot tell you what is under the cursor.

## Requirements

- Windows, with [RustDesk](https://rustdesk.com) installed (found automatically,
  or set `rustdesk_exe`)
- Python 3.11+ — **standard library only**, no dependencies (3.11 for `tomllib`)
- One saved peer, connected to at least once so its password is stored

## Setup

1. Connect to the peer once in RustDesk normally, ticking the option to save the
   password. That is the whole credential setup.
2. Point the bridge at it — `config/bridge.local.json` (gitignored):

   ```json
   { "peer": "123456789" }
   ```

   The tracked `config/bridge.json` leaves `peer` null on purpose: a RustDesk ID
   is the address of a specific machine, and publishing one invites connection
   attempts against it.

3. Check it:

   ```bash
   bin\rdb.cmd doctor
   ```

## Usage

```bash
bin\rdb.cmd status
```

```bash
bin\rdb.cmd open terminal
```

```bash
bin\rdb.cmd forwards --set 2222:localhost:22 --open
```

`doctor` reports whether a credential-free connect is actually possible, what
the remote machine last said about itself (version, whether it supports the
remote terminal, its display layout), and the active policy.

Other commands: `peers`, `sessions`, `info`, `log`, `options`, `policy`,
`option`, `setting`, `geometry`, `calibrate`, `click`, `scroll`, `type`, `press`.

## As an MCP server

```bash
claude mcp add rustdesk -- python -m rdbridge.mcp_server
```

Tool descriptions state which layer each tool belongs to, so the read-only ones
are distinguishable from the ones that contact another machine or send real
input.

## Tests

```bash
python -m unittest discover -s tests
```

All offline — no RustDesk, no peer, no network. 81 tests covering the policy
boundary, the TOML editor's password-preservation guard, log parsing, coordinate
mapping and the shim's exit codes.

## Port forwards expose to the LAN, not to localhost

Confirmed by testing: a forward seeded into the peer config **raises its
listener automatically** when the session opens — no click needed. But RustDesk
binds it to `0.0.0.0`, not `127.0.0.1`:

```
[src\port_forward.rs:59] listening on port 0.0.0.0:14445
```

So the tunnel — and whatever it reaches on the far side — is available to
anything on your local network for as long as the session is open. That is why
`forwards.enabled` is false by default and every forward must be named in
`forwards.allow`. Close the session when you are done with it.

`listening: true` also means only that the socket accepts. A live tunnel to a
dead target looks identical until you send bytes and get none back.

See [`docs/design.md`](docs/design.md) for the rest.
