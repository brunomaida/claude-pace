# pace `template after` and first-use menu

Branch `feature/261015-pace-template-after`. Version 0.12 -> 0.13.

## Goal

1. `/pace template after` adds one pace summary line after the last line of the user's own status line.
2. The first time `/pace` (or a typed `pace ...`) runs after install, pace shows a one-time menu: a ready layout, leave the status line alone, or add the summary line after it.

## Behaviour

### `template after`

`ctl['template']` accepts `AFTER` beside `L1 L2 C1 C2`.

- `template_of(ctl)` still returns only `L1|L2|C1|C2` (`''` for `AFTER`), so every current caller is unchanged.
- `after_of(ctl)` is new: true when `ctl['template'] == 'AFTER'`.
- `PACE_RE` and `parse_pace_prompt` accept `template after` (`tpl-after`).
- `format_status` prints `template after`.

### `statusline --full` contract

```
template   call                          stdout                              script then
---------  ----------------------------  ----------------------------------  -------------------------------
none/off   statusline --full             \x01 + cadence segment              own lines, segment on line 3
L1..C2     statusline --full             whole layout                        prints it, exits
AFTER      statusline --full             \x01 + cadence segment (as none)    own lines, segment on line 3
AFTER      statusline --full --after     \x02 + summary line                 own lines, then the line
```

`--after` is the caller declaring that it handles `\x02`. Without it, `AFTER` behaves like no template. A script that only knows `\x01` is therefore never handed a `\x02` payload it would print as the whole status line. New constant `APPEND_MARK = '\x02'`.

`--standalone` (pace is the whole status line) ignores `AFTER`: nothing precedes the line, so it uses its default layout.

### Summary line

`format_line(dec, ctl, cfg, now, active)`, one line, built from `cadence_fields`:

```
pace [L2] 5h:31% 44>18%/h cut 59% reset 2h10m | wk:41% 4%/h -2%
      |    |    |          |       |             |   |     |
      |    |    |          |       |             |   |     +-- lead vs budget (omitted inside deadband)
      |    |    |          |       |             |   +-------- weekly rate, %/h
      |    |    |          |       |             +------------ weekly used %
      |    |    |          |       +-------------------------- 5h reset, compact (`fmt_left`)
      |    |    |          +---------------------------------- cut = reduce5_pct, only when > 0
      |    |    +--------------------------------------------- now>need %/h when behind, else now %/h
      |    +-------------------------------------------------- 5h used %
      +------------------------------------------------------- state tag (`[ok] [L1] [L2] [L3]`)
```

- An axis appears only when `on` or `measure` (same rule as `format_brief`).
- `[L3]` (5h nearly full): the 5h block is `5h:<used>% reset <left>`; no rate, no cut.
- No sample (`state` `na`/`stale`): `pace <tag> no usage sample yet`, the tag being the segment's (`[off]` while nothing is engaged, `[n/a]` or `[?]` otherwise).
- No axis shown and a sample exists: `pace <tag>`.

### First use

Marker file `<config_dir>/config/pacing.welcomed`. `ctl.json` lives in `%TEMP%/claude-pacing` and is volatile, so it cannot hold this.

On any `/pace` or typed `pace ...`, `cmd_prompt` and `_ctl` append the menu to the output when the marker is absent, then write the marker (the menu shows once):

```
pace: first use. Choose how it shows on your status line:
  /pace template L2      ready layout (L1 L2 C1 C2)
  /pace template off     keep your status line as it is
  /pace template after   add one summary line after yours
```

The menu is skipped, and the marker still written, when the command is `template ...`, `setup` or `setup silent`, or when `ctl['template']` is already set.

### `template after` command output

After setting `AFTER`, the command prints the one change the script needs: pass `--after`, and when the output starts with `\x02` print the rest after the script's own lines. Without that edit the status line stays as it was.

## Failure handling

- Marker write fails: the menu repeats until it can be written. No exception escapes; `cmd_prompt` and `_ctl` are already fail-open.
- `format_line` on a malformed `dec`: falls back to `pace [off]`; no exception.
- Config dir missing: created with `os.makedirs(..., exist_ok=True)` before the marker write.

## Tests (`tests/test_pacing.py`)

| Case | Expectation |
|---|---|
| parse `template after`, `/pace template AFTER` | `tpl-after`; `apply_ctl` sets `AFTER` |
| `template_of({'template':'AFTER'})` | `''` |
| `after_of` | true only for `AFTER` |
| `cmd_statusline_full` AFTER without `--after` | starts with `\x01` |
| `cmd_statusline_full` AFTER with `--after` | starts with `\x02`, single line |
| `--standalone` with AFTER | default layout, no `\x02` |
| `format_line` both axes behind | contains `5h:`, `>`, `cut`, `reset`, `wk:` |
| `format_line` one axis off | that axis absent |
| `format_line` level 3 | no rate, no cut |
| `format_line` no sample | `pace [n/a] no usage sample yet` when an axis is engaged, `pace [off] ...` when none is |
| first use menu | present on first call, absent on second; marker written |
| first use + `template L2` | no menu; marker written |
| first use + `template` already set | no menu; marker written |
| marker unwritable | menu repeats, no exception |

## Docs and version

- `README.md`: Commands table row for `template`, first-use note.
- `docs/pacing-cadencer-readme.html`: the `template` row and the Status line section, EN + PT (`data-pt`).
- `commands/pace.md`: description and `argument-hint` gain `after`.
- 0.13 in `scripts/pacing.py` (`VERSION`), `.claude-plugin/plugin.json` and the manual version badge.

## Out of scope

- A wrapper that replaces `statusLine` and runs the user's original command.
- `AskUserQuestion` in `commands/pace.md`.
- The other four candidate line formats.
- Editing the user's own `statusline-command.sh`.
