# Working in this repo

This bridge can open a session on another machine and, at its top layer, click
things on someone else's desktop. The rules below exist because of that.

## The credential boundary

**Never handle the peer password.** Not in an argument, not in an environment
variable, not in a config file, not "temporarily". RustDesk stores it encrypted;
the bridge names the peer and lets RustDesk resolve it.

Concretely:

- `policy.FORBIDDEN_ARGS` is not configurable and not negotiable. `--password`,
  `--set-unlock-pin`, `--set-id`, `--config`, `--assign`, `--deploy` and every
  install/uninstall verb are permanently unreachable. Do not add an escape hatch,
  a `force` flag, or an "internal use" path.
- `peers.py` strips `password`, `hash`, `salt` and `enc_id` on read. The `Peer`
  record reports `has_saved_password` and nothing more. Do not bind the blob to
  a name, decode it, or expose it behind a flag.
- Credential-bearing keys are refused for **reads** too, not just writes.
  `rustdesk --option key` prints the custom-server key to stdout, and stdout
  ends up in a transcript. That is why `is_secret_key` gates both directions.
- `RustDesk_local.toml` holds an `access_token` for a signed-in RustDesk
  account. `peers.local_state` filters it. Keep it filtered.
- If a task seems to need the password, it needs a peer saved in RustDesk
  instead.

## Layer discipline

Prefer the lowest layer that does the job:

1. **Read the log before believing anything.** A session verb returning 0 means
   "the request was handed to the running instance", not "connected" — RustDesk
   posts a `rustdesk://` URI via a window message and the launcher exits
   immediately. `sessions.open_session` waits for a window and reports what
   actually happened; keep it that way.
2. **A port forward beats clicking, always.** It is a real TCP tunnel to
   anything reachable from the remote machine, which makes the work local. Reach
   for layer 4 only when the task is genuinely GUI-only.
3. **The remote terminal beats both** when the far side is 1.4.1+. Check
   `peer_info()["features"]["terminal"]` — it is in the log.
4. **Never click on an uncalibrated mapping** for anything that matters.
   `geometry_report` says whether the mapping is `calibrated` or a `fit` guess.
   A guess is only right if the view is fitted to the window and not scrolled.

## Traps that have already bitten

- **A minimized window reports a rect at `-32000,-32000`**, not an error. Every
  coordinate derived from it is nonsense. `geometry.assert_usable` catches it;
  do not bypass it.
- **RustDesk holds its config in memory and writes it back on close.** Editing
  `peers/<id>.toml` under a live session silently loses the edit.
  `tomledit.assert_safe_to_edit` refuses; `force=True` is a way to lose work.
- **`--option` needs elevation to *read*, not just to write.** The
  `is_installed() && is_root()` check in `core_main.rs` wraps both branches. So
  reads come from the TOML files and only writes elevate. Do not "simplify" that
  into one elevated path.
- **The four-argument `--port-forward` form is dead on Flutter builds.** The
  Dart parser takes the peer ID and drops the ports. Forwards go into the peer
  config instead.
- **Never round-trip a peer TOML through a parser.** The password is a
  multi-line byte array; re-serialising rewrites it. `tomledit` does surgical
  edits and asserts the password block is byte-identical afterwards.
- **A port forward listens on `0.0.0.0`, not `127.0.0.1`.** It is exposed to the
  whole LAN, along with whatever it reaches on the far side. Close the session
  when the work is done; do not leave forwards open as a convenience.
- **`listening: true` proves a socket, not a service.** A live tunnel to a dead
  target accepts the connection, establishes, and closes with no data. If you
  need to know the far end works, send bytes and check for a reply.
- **Every session kind logs a `PeerInfo`, but only video-bearing ones list
  displays.** Opening a port-forward or file-transfer session writes one with
  `displays: []`, which will mask a live remote desktop's geometry. Use
  `peer_info(require_displays=True)` for anything that needs the screen. This
  already caused one bug; `test_state.py` pins it.

## Before widening the policy

`config/bridge.json` is the safety boundary, not a convenience setting.
Enabling `options.write`, `forwards`, or `input` is a real decision — ask first.
In particular:

- `input.enabled` sends real mouse and keyboard events to another machine. There
  is no undo, no dry run, and no confirmation from the far side.
- `input.allow_keys` is separate from mouse on purpose: a stray keystroke goes
  to whatever has focus over there.
- `options.allow_write` governs global settings. A wrong one can end remote
  access to this machine — `verification-method` and `direct-server` especially.
- Deny beats allow. Keep it that way.

## Tests

`python -m unittest discover -s tests` — all offline, no RustDesk needed. Add a
regression test for any bug you fix in policy, TOML editing, or coordinate
mapping; those three are where a silent failure does real damage.

The password-preservation test in `test_tomledit.py` and the leak test in
`test_peers.py` are load-bearing. Do not weaken them to make a change pass.
