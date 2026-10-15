---
title: "Architecture decisions"
type: adr
solution: claude-pace
status: active
created: 2026-10-15
---

# Architecture decisions

Decisions with lasting consequences for the `pace` plugin. Where each lives in the code: [TOPOLOGY.md](TOPOLOGY.md). Security consequences: [SECURITY.md](../SECURITY.md).

## D1: Every failure is fail-open

#2026-10-14 D1: W: a pacing hook that errors or hangs breaks the user's session, worse than no pacing D: every failure exits 0 with no output; one delay never exceeds cap_s, kept under the hook timeout I: a bug degrades to no pacing; explicit ctl commands still print their failure

## D2: Default is observe-only

#2026-10-14 D2: W: delaying tool calls without consent is a surprise cost D: both axes start OFF; with no ctl file enforce exits at once; pace measure shows the cut and pauses nothing I: nothing changes until /pace on; a user can read the proposal first

## D3: One Python file, standard library only, 3.9 to 3.14

#2026-10-14 D3: W: hook command runs on every tool call on three operating systems with unknown Python setups D: single scripts/pacing.py, stdlib only, no match statement, no runtime X | Y I: no install step; CI matrix has to cover 3.9 to 3.14 and the hook chain falls through py -3, python3, python

## D4: Hot state is volatile, user choices are persistent

#2026-10-14 D4: W: samples and decisions are rewritten many times a minute and go stale fast D: keep them in the temp directory; keep config under the Claude config directory I: switches and template reset when temp is cleaned; config survives

## D5: A plugin cannot ship a status line, /pace setup writes it

#2026-10-14 D5: W: the status line is the only place Claude Code hands pace its usage and a plugin has no way to install one D: /pace setup writes statusLine into settings.json after a backup, never over a status line that is not pace's I: the entry outlives the plugin until removed by hand; documented in the README

## D6: template after is a value that template_of never returns

#2026-10-15 D6: W: add one summary line after the user's own status line without touching callers of the layout API D: ctl template accepts AFTER; template_of still returns only L1, L2, C1, C2; statusline --full --after opts into a new \x02 payload I: scripts without --after are unaffected; the user's script must read the first byte of the reply; the first-use marker lives under the config directory because the state directory is volatile

## D7: State directory is per user and validated on every use

#2026-10-15 D7: W: shared predictable name plus a predictable temp file let a local user redirect a write through a link D: per-user claude-pacing-<uid> directory on POSIX, mode 0700, owner and type checked on each use, exclusive 0600 temp files I: a directory owned by someone else is refused and pace does nothing; upgrading from 0.13 resets the switches once

## D8: allowed-tools rules are anchored to a script path

#2026-10-15 D8: W: a wide wildcard let a -c flag be the first character of the quoted argument D: the quoted path must start with / or a drive letter and a colon I: any file whose path ends in pacing.py still matches; whether the host splits commands before matching is not tested here

---

See also: [TOPOLOGY.md](TOPOLOGY.md).
