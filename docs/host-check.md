---
title: "Host check on macOS and Linux"
type: guide
solution: claude-pace
status: active
created: 2026-10-15
---

# Host check on macOS and Linux

The `test` workflow runs the package suite and the install simulator on Ubuntu, macOS and Windows, and in Debian, Fedora and Alpine containers. It never starts Claude Code itself. This procedure covers what only the real host can show, and is how the "not covered" lines in the [README](../README.md) and the [track record](track-record.md) get closed.

Run it once per platform, record the result table at the end, and paste the rows into the README's host table.

## What it covers

| # | Check | Closes |
|---|---|---|
| 1 | Plugin hooks load and `/pace status` is answered by the prompt hook | host on macOS or Linux |
| 2 | `/pace:pace status` runs the command body with no permission prompt | `allowed-tools` rules under that host's shell |
| 3 | A decided pause is slept for a Bash call and for a Read call | `PreToolUse` on that host |
| 4 | A real subagent takes a slot and releases it | `SubagentStart` and `SubagentStop` |
| 5 | `/pace setup` writes the `statusLine` entry and is safe to run twice | `/pace setup` through the host |
| 6 | `/pace guide` hands the manual to the opener (`open` on macOS, `xdg-open` on Linux) | the browser window |

## Prerequisites

- Claude Code 2.1.80 or later (`claude --version`), logged in.
- Python 3 on `PATH` (`python3 --version`).
- A clone of this repository, here `REPO`.
- A Pro or Max plan is not needed for checks 1 to 4: the decision is seeded by hand, as the install simulator does. It is only needed to see real usage in the status line.

## Isolate the state

Every check uses a throwaway state directory, so nothing touches your own pace state:

```
REPO="$PWD"                                   # the clone
export CLAUDE_PACING_DIR="$(mktemp -d)"       # state: ctl.json, decision.json, agents/, slept.*
chmod 700 "$CLAUDE_PACING_DIR"
CLAUDE_PLUGIN="--plugin-dir $REPO"
```

Check 5 rewrites `settings.json`. Run it against a throwaway Claude config directory (log in once inside it), or back up `~/.claude/settings.json` first; pace also writes `settings.json.pace-backup-<time>` next to it.

## Steps

### 1. Hooks load, prompt hook answers

```
claude $CLAUDE_PLUGIN -p "/pace status"
```

Pass: the reply contains `pace v<VERSION>` (the `VERSION` in `scripts/pacing.py`) and the state directory holds no `pacing.err`.

### 2. Command body runs with no permission prompt

```
claude $CLAUDE_PLUGIN -p "/pace:pace status"
```

Pass: same output as step 1, no permission denial and no prompt. A denial means the host rejected the anchored `allowed-tools` pattern: record the exact message.

### 3. A pause is slept for Bash and Read

Turn pacing on, then seed a fresh decision (it expires after `stale_s`, 600 s, so run the next commands straight after):

```
claude $CLAUDE_PLUGIN -p "/pace on"
python3 - <<'EOF'
import json, os, time
p = os.path.join(os.environ["CLAUDE_PACING_DIR"], "decision.json")
json.dump({"ts": time.time(), "state": "L1", "level": 1, "delay_rec": 1.2}, open(p, "w"))
EOF
time claude $CLAUDE_PLUGIN -p "Run the Bash command 'true', then read README.md, then say done."
ls "$CLAUDE_PACING_DIR"
```

Pass: a `slept.*` file exists in the state directory, the run took longer than 1.2 s per call, and there is no `pacing.err`. In `-p` mode the host may deny the Bash call for lack of permission; the pause still counts, because `PreToolUse` runs before the permission decision. Add `--allowedTools` to silence the denial.

### 4. A subagent takes and releases a slot

Seed a level 2 decision, poll the slot directory in the background, then spawn one subagent:

```
python3 - <<'EOF'
import json, os, time
p = os.path.join(os.environ["CLAUDE_PACING_DIR"], "decision.json")
json.dump({"ts": time.time(), "state": "L2", "level": 2, "delay_rec": 0}, open(p, "w"))
EOF
( for i in $(seq 1 240); do ls "$CLAUDE_PACING_DIR/agents" 2>/dev/null; sleep 0.5; done ) > /tmp/pace-slots.txt &
claude $CLAUDE_PLUGIN -p "Use the Agent tool once with subagent_type general-purpose to answer OK, then say done."
wait
sort -u /tmp/pace-slots.txt
ls "$CLAUDE_PACING_DIR/agents"
```

Pass: the poll shows a `.p` file (claimed) and a `.a` file (confirmed) at some point, and the directory is empty at the end.

### 5. `/pace setup` through the host

In a throwaway config directory:

```
export CLAUDE_CONFIG_DIR="$(mktemp -d)"       # log in once inside it
claude $CLAUDE_PLUGIN -p "/pace:pace setup silent"
cat "$CLAUDE_CONFIG_DIR/settings.json"
claude $CLAUDE_PLUGIN -p "/pace:pace setup silent"
```

Pass: the first run prints `statusLine set in .../settings.json`, `settings.json` holds a `statusLine` entry whose command runs `$CLAUDE_CONFIG_DIR/pace/pacing.py`, that file exists, and the second run prints `statusLine already points at pace: nothing changed.` Then repeat once with your own `statusLine` already present: the reply must start `statusLine kept (it is yours):` and print the one line to add to your script.

### 6. `/pace guide` opens the manual

From a desktop session (not SSH):

```
claude $CLAUDE_PLUGIN -p "/pace:pace guide"
```

Pass: the reply prints the path and a `file://` link, and the manual opens in the default browser; the last line reads `Asked the desktop to open it.` With no opener available (a headless Linux box) the last line reads `Open the link above in a browser.` instead; record which one happened.

## Clean up

```
unset CLAUDE_PACING_DIR CLAUDE_CONFIG_DIR
rm -rf "$CLAUDE_PACING_DIR" /tmp/pace-slots.txt
```

## Record the result

One row per check and platform, with exact versions:

| Host | Claude Code | Python | Check | Result | Note |
|---|---|---|---|---|---|
| macOS (version, chip) | `claude --version` | `python3 --version` | 1 | pass / fail | |
| Linux (distro, version) | | | 1 | | |

Add the passing rows to the host table in the README and remove the matching line from "Not covered" there and in the [track record](track-record.md). A failing row is an issue first: do not edit the README until it is fixed or documented as a limit.
