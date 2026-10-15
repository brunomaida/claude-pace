# Security

## Reporting

Report a vulnerability privately through GitHub: <https://github.com/brunomaida/claude-pace/security/advisories/new>. Please do not open a public issue for it.

## What pace touches

| Surface | Detail |
|---|---|
| Hooks | `PreToolUse`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop` run `scripts/pacing.py` on every tool call and prompt. A pause never exceeds `cap_s` (at most 55 s, under the 60 s hook timeout) and every failure is fail-open. |
| Status line | `/pace setup` writes a `statusLine` command into `~/.claude/settings.json` (backed up first, never over a status line that is not pace's) and copies the script to `~/.claude/pace/pacing.py`. |
| State | Usage samples, the decision and slot files live in a temp directory: `%TEMP%\claude-pacing` on Windows, `claude-pacing-<uid>` in the temp directory on Linux and macOS, mode 0700. |
| Config | `~/.claude/config/pacing.config.json` and the first-use marker `~/.claude/config/pacing.welcomed`. |
| Network | None. The scripts open no connection, and the manual (`/pace guide`) loads no font, script or image from the network: it uses the system fonts (Inter and JetBrains Mono when installed). |

## Trust model

- The user's own account and environment are trusted: `CLAUDE_PACING_DIR`, `CLAUDE_PACING_CONFIG` and `CLAUDE_CONFIG_DIR` redirect where pace reads and writes.
- Other local users are not trusted. On POSIX the state directory must be the caller's own real directory: a link, a file or a directory owned by someone else is refused, and pace then does nothing (a `ctl` command prints `pace ctl failed: PermissionError`). The default directory, when it is the caller's own with a loose mode, is tightened to 0700; one named by `CLAUDE_PACING_DIR` is validated but its mode is left to you.
- State files are not trusted either: a decision dated in the future, a state tag pace never writes, or a rate table with non-numeric keys is ignored; slot files dated in the future are dropped; session ids are reduced to a plain token before they become file names.
- Files written through `_write_text` (state, settings) go through an exclusive 0600 temp file, so a planted name or link is never followed. Slot files and the agents lock are created with your umask: in the default directory they are protected by its 0700 mode, in a `CLAUDE_PACING_DIR` they are as private as that directory.

## Known limits

- `commands/pace.md` allows `python3`, `python` and `py -3` with `-S "<path>pacing.py" ctl ...` so the command runs without a prompt. The quoted argument must start with `/` (POSIX path) or with a character and a colon (a Windows drive), so a `-c` or `-m` flag cannot be the first character: `-cimport os#pacing.py` does not match, and the one string that does start `-:` is refused by Python. Any file whose path ends in `pacing.py` still matches, and a rule is a pattern, not a parse: whether Claude Code splits a command at `;`, `&&` or `$(...)` before it applies the pattern is a property of the host that this repo does not test. The rules are checked against the real matcher only by the host checks in the README, not by the test suite. If a host rejects a form you get a permission prompt, not a wider rule.
- Removing the plugin does not remove the `statusLine` entry that `/pace setup` added; until you delete it (see the README), the status line keeps running `~/.claude/pace/pacing.py` and writing state.
- On a shared Linux host the default name `claude-pacing-<uid>` is predictable: another user can create it first. Pace then refuses it and does nothing (a standalone status line is blank, `/pace ...` prints `pace ctl failed`). Point `CLAUDE_PACING_DIR` at a directory under your home or `$XDG_RUNTIME_DIR` to avoid it.
- Upgrading from 0.13 on Linux and macOS leaves the old state directory behind: the on/off switches and the template are reset once. Run `/pace on` again.
- On Windows, `/pace setup` refuses a config dir or Python path that contains `$`, a backtick or a double quote, because Git Bash and PowerShell escape them differently.
