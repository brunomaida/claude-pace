# pace

A Claude Code plugin that spreads your subscription use over the week and over the 5-hour window. It watches the usage Claude Code reports, works out how fast you are going against how fast you can afford to go, and adds a short, bounded delay to tool calls when you are ahead. It can also cap how many subagents run at once. Nothing is throttled until you turn it on.

## Requirements

| You need | Why |
|---|---|
| Claude Code 2.1.80 or later | the status line input carries `rate_limits` from that version; without it pace has no data |
| A Claude Pro or Max plan | `rate_limits` is only sent for those plans; API-key sessions stay `[n/a]` |
| Python 3 on `PATH` (`python3`, `python`, or `py -3`) | every hook and the `/pace` command are one Python file |
| Git for Windows, on Windows | Claude Code runs hook commands through Git Bash when it is installed and through PowerShell otherwise (hooks documentation); the hook commands use `||`, which Windows PowerShell 5.1 does not parse (see Limits) |

## Install

```
/plugin marketplace add brunomaida/claude-pace
/plugin install pace@claude-pace
```

Then, once:

```
/pace setup
/pace on
```

`/pace setup` is needed because a plugin cannot install a status line, and the status line is the only place Claude Code hands pace your usage. It is safe to run twice:

- No status line yet: pace writes one that shows its own line (`/pace setup silent` writes one that records and prints nothing).
- You already have a status line: it is kept, and pace prints the one line to add to your script.
- `settings.json` is backed up first and left alone if it is not valid JSON.

Axes start OFF (observe only). `/pace on` turns both on; `/pace wk on` and `/pace 5h on` turn one.

## Commands

| Command | Does |
|---|---|
| `/pace status` | state, rate against budget, current delay and subagent cap |
| `/pace brief` | one line per axis, short enough for a narrow client |
| `/pace guide` | opens the manual in your browser, or prints its path in a remote session |
| `/pace [wk\|5h] on` / `off` | apply or stop pacing, per axis or both |
| `/pace [wk\|5h] measure [on\|off]` | show the proposed reduction without applying it |
| `/pace up` / `down` / `auto` / `reset` | make the cadence stricter or looser, or return to the suggestion |
| `/pace template L1\|L2\|C1\|C2\|off` | choose the status line layout |
| `/pace setup [silent]` | install the status line entry |

You can also type `pace on`, `pace status` and the rest without the slash; the prompt hook handles them.

## The manual

`/pace guide` opens `docs/pacing-cadencer-readme.html` from the installed plugin: how the budget and the two axes work, every command, the configuration keys and the status line layouts. The manual's install section describes copying the files into `~/.claude` by hand, which is the route for people who do not use the plugin system.

## Configuration

Optional. Put keys in `~/.claude/config/pacing.config.json` (or point `CLAUDE_PACING_CONFIG` at a file); unknown keys are ignored.

| Key | Default | Meaning |
|---|---|---|
| `ceiling_pct` | `95` | share of the weekly quota the budget may reach |
| `cap_s` | `45` | longest single delay, in seconds |
| `n_max` | `2` | concurrent subagents at level 2 and above |
| `workdays` | `[0,1,2,3,4]` | days the weekly budget is spread over (Monday is 0) |
| `hours` | `[0,24]` | active hours inside a day |
| `exempt_agents` | `[]` | subagent types that are never counted or capped |
| `exempt_tools` | `["AskUserQuestion","ExitPlanMode"]` | tools that are never delayed |

The full list is `DEFAULTS` at the top of `scripts/pacing.py`.

## Updating and removing

After a plugin update, pace refreshes the copy of the script that the status line runs on your next prompt, so there is nothing to redo.

To remove it:

```
/plugin uninstall pace@claude-pace
```

then delete the `statusLine` entry that `/pace setup` added to `~/.claude/settings.json` (the backup next to it is named `settings.json.pace-backup-<time>`) and the folder `~/.claude/pace/`. State lives in your system temp directory under `claude-pacing` and holds no secrets.

## Tested on

Only what has actually been run is listed. The package carries an install simulator (`tests/test_install.py`) that runs the hook commands and the command line through the shells and Python launchers present on the machine, in a profile whose path has a space and an accent.

| OS | Shell | Python | Result |
|---|---|---|---|
| Windows 11 | Git Bash | 3.12.5 | pass: chain as shipped, `python`, `py -3`, absolute path, first interpreter missing |
| Windows 11 | Windows PowerShell 5.1 | 3.12.5 | pass for `python`, `py -3` and the absolute interpreter path; the `a \|\| b` chain does not parse (see Limits). `python3` and PowerShell 7 were not available to run |
| macOS, Linux | `sh` | 3.9, 3.12, 3.13 | run by the CI workflow in `.github/workflows/test.yml`; not recorded here until it has run |

## Limits

- No status line, no data. Without `/pace setup` (or your own status line feeding pace) there are no samples, and pace stays inert.
- API-key sessions send no usage data; `/pace status` says so.
- Hook commands try `py -3`, then `python3`, then `python`, using `||`, and end in `exit 1`. Windows PowerShell 5.1 has no `||`, so on a Windows machine with PowerShell 5.1 and no Git for Windows the hooks fail to start; install Git for Windows. PowerShell 7 parses `||` but has not been tested here.
- Python missing from `PATH` makes every hook and `/pace` fail visibly and nothing is throttled.
- `/pace on` delays tool calls by design; pacing never exceeds `cap_s` per model turn and keeps the gap short enough to stay inside the prompt cache.

## License

MIT, see `LICENSE`.
