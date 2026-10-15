---
title: "Topology"
type: topology
solution: claude-pace
status: active
created: 2026-10-15
---

# Topology

How the `pace` plugin is put together. Decisions behind it: [architecture-decisions.md](architecture-decisions.md). Usage and install: [README](../README.md). Threat model: [SECURITY.md](../SECURITY.md).

## Assembly Roles

| Path | Role |
|---|---|
| `.claude-plugin/plugin.json` | plugin manifest: name `pace`, version, license, repository |
| `.claude-plugin/marketplace.json` | one-plugin marketplace, so the same repo is installed with `/plugin marketplace add` |
| `hooks/hooks.json` | registers four hooks (`PreToolUse`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`); each tries `py -3`, `python3`, `python` in that order |
| `commands/pace.md` | the `/pace` slash command: runs `pacing.py ctl $ARGUMENTS`; `allowed-tools` rules are anchored to the script path |
| `scripts/pacing.py` | the whole runtime in one file: decision engine, state files, `ctl` CLI, status line rendering, `setup` |
| `docs/pacing-cadencer-readme.html` | the manual opened by `/pace guide` |
| `tests/test_pacing.py` | unit tests of the decision engine, rendering, hardening and CLI |
| `tests/test_install.py` | install simulator: runs the shipped hook chain and command through the shells and Python launchers present on the machine |
| `.github/workflows/test.yml` | CI: 3 operating systems × Python 3.9 to 3.14, plus Debian, Fedora and Alpine containers |

## Runtime data

| Data | Where | Lifetime |
|---|---|---|
| samples, decision, subagent slots, on/off switches, template | `%TEMP%\claude-pacing` on Windows; `claude-pacing-<uid>` (mode 0700) in the temp directory on Linux and macOS | volatile |
| config keys | `~/.claude/config/pacing.config.json` (or `CLAUDE_PACING_CONFIG`) | persistent, optional |
| first-use marker | `~/.claude/config/pacing.welcomed` | persistent |
| stable copy of the script for the status line | `~/.claude/pace/pacing.py` | refreshed on the next prompt after a plugin update |
| `statusLine` entry | `~/.claude/settings.json`, written by `/pace setup` (backup first) | stays after uninstall until removed by hand |

## Dependency Direction Graph

```
Claude Code host
  │  hook events (PreToolUse, UserPromptSubmit, SubagentStart, SubagentStop)
  ▼
hooks/hooks.json ──► scripts/pacing.py ◄── commands/pace.md  (/pace → ctl)
                        │      ▲
        statusLine JSON │      │ pace template / setup
        (rate_limits)   ▼      │
              ~/.claude/pace/pacing.py ── status line ── user's own script
                        │
                        ▼
              temp state dir  ·  ~/.claude/config/
```

`scripts/pacing.py` imports only the Python standard library. Nothing else in the repo is imported by it.

<!-- doc-links:auto -->
## Related

- [Architecture decisions](architecture-decisions.md)
