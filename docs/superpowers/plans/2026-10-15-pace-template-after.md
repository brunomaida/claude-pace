# pace `template after` and first-use menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/pace template after` appends one pace summary line after the user's own status line, and the first `/pace` after install shows a one-time menu of the three status line choices.

**Architecture:** `after` is a template value that `template_of()` never returns, so every current caller is untouched. `statusline --full --after` is the caller's opt-in to a new `\x02` payload; without the flag the output is the old `\x01` segment. The first-use marker is a file under `<config_dir>/config/`, because the state dir is in `%TEMP%` and volatile.

**Tech Stack:** Python 3.9 to 3.14 stdlib only, `unittest`.

**Spec:** [`docs/superpowers/specs/2026-10-15-pace-template-after-design.md`](../specs/2026-10-15-pace-template-after-design.md). Issue: brunomaida/claude-pace#1.

## Global Constraints

- Python 3.9+ (CI matrix 3.9 to 3.14): no `match`, no runtime `X | Y`; annotations are fine (`from __future__ import annotations` is already in `scripts/pacing.py`).
- Standard library only; no new files under `scripts/`.
- Fail-open: nothing raises out of a hook or `ctl`; exit code stays 0.
- Default stays OFF: no behaviour change for a user who never runs `template after`.
- Commits: Conventional Commits in English, `(#1)` suffix, no `Co-Authored-By`, no AI attribution. Stage files by name, never `scripts/__pycache__/`.
- Test names follow the file: `test_<subject>_<behaviour>`.
- Run tests from `C:\Users\bruno\claude-pace` with `python -m unittest discover -s tests -p "test_pacing.py" -k <pattern> -v`.

## Review Focus

- A script that predates `--after` while `template` is `AFTER`: must still receive the `\x01` segment, never `\x02` (test in Task 3).
- `--standalone` with `AFTER` chosen: pace is the whole line, so it prints the layout and no mark (Task 3).
- `<config_dir>/config` unwritable: the menu repeats and nothing raises (Task 4).
- First command is `template ...` or `setup`, or a template is already set: no menu, marker written (Task 4).
- Partial or missing `dec` (no sample, level 3, no `level` key): a readable line, no exception (Task 2).

---

### Task 1: `after` as a template value

**Files:**
- Modify: `scripts/pacing.py` (`PACE_RE` at line 61, `template_of` at line 473, `format_status` at line 404, module docstring line 14)
- Test: `tests/test_pacing.py` (class `Templates`, after `test_promptParse_template`)

**Interfaces:**
- Produces: `after_of(ctl: dict) -> bool`; `parse_pace_prompt('/pace template after') == 'tpl-after'`; `apply_ctl('tpl-after', {}, cfg)['template'] == 'AFTER'`; `template_of` still returns `''` for `AFTER`.

- [ ] **Step 1: Write the failing tests**

Insert in class `Templates` of `tests/test_pacing.py`, directly after `test_promptParse_template`:

```python
    def test_promptParse_templateAfter(self):
        p = pacing.parse_pace_prompt
        self.assertEqual([p('/pace template after'), p('pace TEMPLATE After')], ['tpl-after', 'tpl-after'])
        self.assertEqual(pacing.apply_ctl('tpl-after', {}, CFG)['template'], 'AFTER')

    def test_afterIsNotAFullLayout(self):
        self.assertEqual(pacing.template_of({'template': 'AFTER'}), '')
        self.assertTrue(pacing.after_of({'template': 'AFTER'}))
        self.assertEqual([pacing.after_of({}), pacing.after_of({'template': 'L2'}), pacing.after_of({'template': 'off'})],
                         [False, False, False])

    def test_statusNamesTheAfterTemplate(self):
        out = pacing.format_status({'state': 'na'}, {'template': 'AFTER'}, CFG, NOW)
        self.assertIn('| template after |', out)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k templateAfter -k afterIsNot -k statusNamesTheAfter -v`
Expected: FAIL (`parse_pace_prompt` returns `None`; `after_of` is not defined).

- [ ] **Step 3: Implement**

In `scripts/pacing.py`, `PACE_RE` (lines 61-63): accept `after`:

```python
    r'|(on|off|up|down|auto|reset|status|brief|guide|setup(?:\s+silent)?|measure(?:\s+(?:on|off))?|template(?:\s+(?:l1|l2|c1|c2|off|after))?)))?\s*$', re.I)
```

After `template_of` (line 476) add:

```python
def after_of(ctl: dict) -> bool:
    """`pace template after`: the summary line goes after the shell's own lines instead of a layout."""
    return str(ctl.get('template') or '').upper() == 'AFTER'
```

In `format_status`, replace the template bit:

```python
            f'template {template_of(c) or ("after" if after_of(c) else "off")}', f'mode {"manual" if is_manual(c, cfg) else "auto"}',
```

In the module docstring, change the `ctl <cmd>` line to name `after`:

```
  ctl <cmd>    CLI: [wk|5h] on | off | measure [on|off]; up | down | auto | reset | status | guide | template <name>|after|off | setup
```

- [ ] **Step 4: Run to verify they pass, then the whole class**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k Templates -v`
Expected: PASS, including the older `test_promptParse_template` and `test_templateOf_noneByDefaultAndForUnknownOrOff`.

- [ ] **Step 5: Commit**

```bash
git add scripts/pacing.py tests/test_pacing.py
git commit -m "feat: accept template after as a status line choice (#1)"
```

---

### Task 2: the summary line

**Files:**
- Modify: `scripts/pacing.py` (new `format_line`, directly after `format_brief`, before the `# ---- status line templates` banner)
- Test: `tests/test_pacing.py` (new class `SummaryLine`, after class `Brief`)

**Interfaces:**
- Consumes: `cadence_fields`, `norm_ctl`, `fmt_left(secs, compact)` (existing).
- Produces: `format_line(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str`, one line, no `\n`.

- [ ] **Step 1: Write the failing tests**

Insert after class `Brief`:

```python
class SummaryLine(unittest.TestCase):
    """`template after`: one line, same vocabulary as the `cadence:` segment, printed after the user's lines."""
    _dec = Brief._dec
    _ctl = Brief._ctl

    def _line(self, dec=None, ctl=None):
        return pacing.format_line(dec or self._dec(), self._ctl() if ctl is None else ctl, CFG, NOW)

    def test_h5ActingWeekMeasuring_showsBothBlocks(self):
        self.assertEqual(self._line(), 'pace [L1] 5h:24% 18>21%/h reset 1h50m | wk:45% 2%/h -25%')

    def test_behindOnBoth_showsCutsAndTheWeeklyDeviation(self):
        dec = self._dec(lead=8.0, deficit5=2.0, now_rate5=44.0, need_rate5=18.0, reduce5_pct=59, u5=31, u7=41,
                        now_rate=4.0, need_rate=3.0)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))),
                         'pace [L1] 5h:31% 44>18%/h cut 59% reset 1h50m | wk:41% 4>3%/h cut 10% +8%')

    def test_leadInsideTheDeadband_isLeftOut(self):
        dec = self._dec(lead=-2.0, deficit5=2.0, now_rate5=44.0, need_rate5=18.0, reduce5_pct=59, u5=31, u7=41,
                        now_rate=4.2, reduce_pct=0)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))),
                         'pace [L1] 5h:31% 44>18%/h cut 59% reset 1h50m | wk:41% 4%/h')

    def test_level3_hasNoRateAndNoCut(self):
        dec = self._dec(state='L3', level=3, u5=97, r5=NOW + 720)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))), 'pace [L3] 5h:97% reset 12m | wk:45% -25%')

    def test_axisWithBothOffIsLeftOut(self):
        self.assertEqual(self._line(ctl=self._ctl(wk=(False, False))), 'pace [L1] 5h:24% 18>21%/h reset 1h50m')

    def test_nothingEngaged_isTheTagOnly(self):
        self.assertEqual(self._line(self._dec(state='ok', level=0, lead=0.0), {}), 'pace [off]')

    def test_noSample_saysSoWithTheSegmentTag(self):
        self.assertEqual(self._line({'state': 'na'}, self._ctl()), 'pace [n/a] no usage sample yet')
        self.assertEqual(self._line({'state': 'na'}, {}), 'pace [off] no usage sample yet')

    def test_partialDecision_neverRaises(self):
        self.assertEqual(self._line({'state': 'L1'}, self._ctl()), 'pace [off]')

    def test_isOneLine(self):
        self.assertNotIn('\n', self._line())
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k SummaryLine -v`
Expected: FAIL (`module 'pacing' has no attribute 'format_line'`).

- [ ] **Step 3: Implement**

In `scripts/pacing.py`, directly after `format_brief` and before `# ---- status line templates`:

```python
def format_line(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    """One line for `template after`: the state tag, then a 5h and a week block for the observed axes."""
    try:
        c = norm_ctl(ctl)
        f = cadence_fields(dec, c, cfg, now, active)
        head = 'pace ' + f['tag']
        if dec.get('state') in ('na', 'stale'):
            return head + ' no usage sample yet'
        blocks = []
        for ax, label, used_k, reset_k in (('h5', '5h', 'u5', 'r5'), ('wk', 'wk', 'u7', None)):
            if not (c[ax]['on'] or c[ax]['measure']):
                continue
            items = []
            if dec.get(used_k) is not None:
                items.append(f'{dec[used_k]:.0f}%')
            if f[ax]:
                rate, cut = f[ax]
                items.append(f'{rate}%/h')
                if cut:
                    items.append('cut ' + cut.lstrip('-'))
            if ax == 'wk' and f['lead']:
                items.append(f['lead'])
            if reset_k and dec.get(reset_k):
                items.append('reset ' + fmt_left(max(0, dec[reset_k] - now), True))
            if items:
                blocks.append(f'{label}:' + ' '.join(items))
        return head + (' ' + ' | '.join(blocks) if blocks else '')
    except (KeyError, TypeError, ValueError):
        return 'pace [off]'
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k SummaryLine -k Brief -v`
Expected: PASS (the `Brief` class is unchanged).

- [ ] **Step 5: Commit**

```bash
git add scripts/pacing.py tests/test_pacing.py
git commit -m "feat: add the one-line summary for template after (#1)"
```

---

### Task 3: the `--after` contract

**Files:**
- Modify: `scripts/pacing.py` (constant next to `SEGMENT_MARK` at line 65; `cmd_statusline_full` at line 857; `main` at line 1204; module docstring lines 5-9)
- Test: `tests/test_pacing.py` (new class `AfterContract` before `class Cli`; one test in class `Cli`)

**Interfaces:**
- Consumes: `after_of`, `format_line`, `template_of`.
- Produces: `APPEND_MARK = '\x02'`; `cmd_statusline_full(raw, cfg, now, git, default_template='', after=False) -> str`.

- [ ] **Step 1: Write the failing tests**

Insert before `class Cli`:

```python
class AfterContract(unittest.TestCase):
    """`statusline --full --after`: the caller says it understands APPEND_MARK; without it nothing changes."""
    RAW = {'session_id': 's', 'model': {'display_name': 'Claude Opus'},
           'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': 1791648600},
                           'seven_day': {'used_percentage': 71, 'resets_at': RESET7}}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self.tmp.name) / 'state'
        self.sd.mkdir()
        self.env = mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': str(self.sd)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _full(self, template, after, default=''):
        ctl = {'wk': {'on': True}}
        if template:
            ctl['template'] = template
        (self.sd / 'ctl.json').write_text(json.dumps(ctl), encoding='utf-8')
        return pacing.cmd_statusline_full(self.RAW, CFG, NOW, '', default, after)

    def test_afterWithTheFlag_isTheAppendMarkAndOneLine(self):
        out = self._full('AFTER', True)
        self.assertTrue(out.startswith(pacing.APPEND_MARK + 'pace ['), out)
        self.assertNotIn('\n', out)

    def test_afterWithoutTheFlag_isTheOldSegment(self):
        out = self._full('AFTER', False)
        self.assertTrue(out.startswith(pacing.SEGMENT_MARK + 'cadence:'), out)

    def test_noTemplateWithTheFlag_isTheOldSegment(self):
        self.assertTrue(self._full('', True).startswith(pacing.SEGMENT_MARK + 'cadence:'))

    def test_aFullLayoutWinsOverTheFlag(self):
        out = self._full('L2', True)
        self.assertNotIn(pacing.APPEND_MARK, out)
        self.assertNotIn(pacing.SEGMENT_MARK, out)

    def test_standalone_ignoresAfter(self):
        out = self._full('AFTER', True, default='L1')
        self.assertNotIn(pacing.APPEND_MARK, out)
        self.assertNotIn(pacing.SEGMENT_MARK, out)
        self.assertIn('Opus', out)
```

Insert in class `Cli`, after `test_statusline_recordOnly_printsNothingButSamplesAndDecides`:

```python
    def test_statuslineFullAfter_printsTheAppendMark(self):
        (Path(self.tmp.name) / 'ctl.json').write_text(json.dumps({'template': 'AFTER', 'wk': {'on': True}}),
                                                      encoding='utf-8')
        code, out = self._run('statusline', self._status(), '--full', '--after')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('\x02pace ['), repr(out))
        self.assertNotIn('\n', out)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k AfterContract -k statuslineFullAfter -v`
Expected: FAIL (`APPEND_MARK` is not defined; `cmd_statusline_full` takes no `after`).

- [ ] **Step 3: Implement**

After `SEGMENT_MARK` (line 65) add:

```python
APPEND_MARK = '\x02'                  # `statusline --full --after` prefix: the rest is one line for after the shell's own
```

Replace `cmd_statusline_full` (lines 857-868):

```python
def cmd_statusline_full(raw: dict, cfg: dict, now: float, git: str, default_template: str = '',
                        after: bool = False) -> str:
    """The whole status line in the template chosen with `pace template`; `git` is the repo text from the shell.

    With no template it returns the `cadence: ...` segment behind SEGMENT_MARK instead, so the shell keeps its
    own layout, starts python once per refresh, and a model name can never be mistaken for the segment.
    `default_template` is the layout used when none is chosen (`--standalone`: pace is the whole status line).
    `after` is the caller declaring it understands APPEND_MARK: with `template after` chosen the reply is
    APPEND_MARK + one summary line for the shell to print after its own lines. Without the flag `after` behaves
    like no template, so a script written before it never prints a payload it does not know.
    """
    dec, ctl, active = _statusline_state(raw, cfg, now)
    name = template_of(ctl) or default_template
    if not name:
        if after and after_of(ctl):
            return APPEND_MARK + format_line(dec, ctl, cfg, now, active)
        return SEGMENT_MARK + render_segment(dec, ctl, cfg, now, active)
    return render_template(name, view_from_raw(raw, git, now), dec, ctl, cfg, now, active)
```

In `main`, replace the `--full` branch call:

```python
                print(cmd_statusline_full(raw, cfg, now, os.environ.get('CLAUDE_STATUS_GIT', ''),
                                          'L1' if '--standalone' in argv[2:] else '', '--after' in argv[2:]))
```

In the module docstring, after the `--full` paragraph (after the line ending `... as volatile as the on/off switches`) add:

```
               `--full --after`: with `pace template after` the reply is `\x02` + one summary line, for a shell that
               prints it after its own lines; without `--after` the reply is the `\x01` segment as before
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k AfterContract -k statuslineFull -k standalone -v`
Expected: PASS, including the older `test_standaloneFull_withoutTemplate_printsAnL1LayoutNoMarker`.

- [ ] **Step 5: Commit**

```bash
git add scripts/pacing.py tests/test_pacing.py
git commit -m "feat: statusline --full --after emits the summary line behind a new mark (#1)"
```

---

### Task 4: first-use menu and the `after` note

**Files:**
- Modify: `scripts/pacing.py` (constants after `NO_DATA_HINT`; new `welcome_marker`, `first_use_menu`, `_reply` directly before `cmd_prompt`; `cmd_prompt`; `_ctl`)
- Test: `tests/test_pacing.py` (imports line 3; test-profile isolation after the imports; new class `FirstUse` after `AfterContract`)

**Interfaces:**
- Consumes: `config_dir`, `_write_text`, `read_ctl`.
- Produces: `welcome_marker() -> str`; `first_use_menu(cmd: str, ctl: dict) -> str`; `_reply(cmd, ctl, text) -> str`; constants `WELCOME_MENU`, `AFTER_NOTE`.

- [ ] **Step 1: Isolate the test profile**

The suite must never write the marker into a real `~/.claude`, and no existing test should see the menu. At the top of `tests/test_pacing.py` change the import line to:

```python
import contextlib, datetime, io, json, os, subprocess, sys, tempfile, time, unittest
```

and directly after `import pacing  # noqa: E402` add:

```python
# A pre-written first-use marker in a private config dir: no test below sees the menu, and none touches ~/.claude.
_CFG = tempfile.TemporaryDirectory()
(Path(_CFG.name) / 'config').mkdir()
(Path(_CFG.name) / 'config' / 'pacing.welcomed').write_text('0', encoding='utf-8')
os.environ['CLAUDE_CONFIG_DIR'] = _CFG.name
```

Run: `python -m unittest discover -s tests -p "test_pacing.py"`
Expected: PASS (nothing else changed yet).

- [ ] **Step 2: Write the failing tests**

Insert class `FirstUse` after `AfterContract`:

```python
class FirstUse(unittest.TestCase):
    """The one-time menu and the `after` note that a command's reply can carry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.marker = self.root / 'config' / 'pacing.welcomed'
        self.env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root),
                                                'CLAUDE_PACING_DIR': str(self.root / 'state')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _reason(self, prompt):
        return pacing.cmd_prompt({'prompt': prompt}, CFG, NOW)['reason']

    def test_firstCommand_showsTheMenuOnceAndWritesTheMarker(self):
        first = self._reason('/pace status')
        self.assertIn('first use', first)
        for choice in ('/pace template L2', '/pace template off', '/pace template after'):
            self.assertIn(choice, first)
        self.assertTrue(self.marker.exists())
        self.assertNotIn('first use', self._reason('/pace status'))

    def test_typedForm_showsItToo(self):
        self.assertIn('first use', self._reason('pace on'))

    def test_templateChoiceFirst_marksWithoutTheMenu(self):
        self.assertNotIn('first use', self._reason('/pace template L2'))
        self.assertTrue(self.marker.exists())

    def test_setupFirst_marksWithoutTheMenu(self):
        self.assertNotIn('first use', self._reason('/pace setup'))
        self.assertTrue(self.marker.exists())

    def test_templateAlreadySet_marksWithoutTheMenu(self):
        (self.root / 'state').mkdir()
        (self.root / 'state' / 'ctl.json').write_text(json.dumps({'template': 'L1'}), encoding='utf-8')
        self.assertNotIn('first use', self._reason('/pace status'))
        self.assertTrue(self.marker.exists())

    def test_unwritableMarker_repeatsTheMenuWithoutRaising(self):
        (self.root / 'config').write_text('a file where the directory should be', encoding='utf-8')
        self.assertIn('first use', self._reason('/pace status'))
        self.assertIn('first use', self._reason('/pace status'))

    def test_afterCommand_printsTheScriptNote(self):
        reason = self._reason('/pace template after')
        self.assertIn('--after', reason)
        self.assertIn('x02', reason)
        self.assertNotIn('first use', reason)

    def test_ctlPath_showsTheMenuOnceToo(self):
        def run():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(pacing._ctl(['pacing.py', 'ctl', 'status'], CFG, NOW), 0)
            return buf.getvalue()
        self.assertIn('first use', run())
        self.assertNotIn('first use', run())
```

- [ ] **Step 3: Run to verify they fail**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k FirstUse -v`
Expected: FAIL (no menu in the reply).

- [ ] **Step 4: Implement**

In `scripts/pacing.py`, after `NO_DATA_HINT` (line 66) add:

```python
WELCOME_MENU = '\n'.join((
    'pace: first use. Choose how it shows on your status line:',
    '  /pace template L2      ready layout (L1 L2 C1 C2)',
    '  /pace template off     keep your status line as it is',
    '  /pace template after   add one summary line after yours'))
AFTER_NOTE = ('pace template after: pass --after to `statusline --full` in your script and, when the output '
              'starts with \\x02, print the rest after your own lines. Until then nothing changes.')
```

Directly before `def cmd_prompt` add:

```python
def welcome_marker() -> str:
    """Persistent (config dir), unlike ctl.json: a temp-dir marker would bring the menu back after every reboot."""
    return os.path.join(config_dir(), 'config', 'pacing.welcomed')


def first_use_menu(cmd: str, ctl: dict) -> str:
    """The one-time menu for the first `/pace`, or ''. A template choice, `setup` or a template already set are
    the answer itself: they only write the marker. A marker that cannot be written leaves the menu showing."""
    marker = welcome_marker()
    if os.path.exists(marker):
        return ''
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        _write_text(marker, str(int(time.time())))
    except OSError:
        pass
    answered = cmd.startswith('tpl-') or cmd in ('setup', 'setup-silent') or bool(ctl.get('template'))
    return '' if answered else WELCOME_MENU


def _reply(cmd: str, ctl: dict, text: str) -> str:
    """A command's output plus what only some commands add: the `after` script note, the first-use menu."""
    extra = [AFTER_NOTE] if cmd == 'tpl-after' else []
    menu = first_use_menu(cmd, ctl)
    if menu:
        extra.append(menu)
    return '\n'.join([text] + extra)
```

In `cmd_prompt`, replace the setup branch and the final reply:

```python
    if cmd in ('setup', 'setup-silent'):
        first_use_menu(cmd, ctl)  # `setup` is one of the menu's answers: it marks, it shows nothing
        return {'decision': 'block', 'reason': cmd_setup(config_dir(), __file__, cmd == 'setup-silent')}
```

```python
        show = format_brief if cmd == 'brief' else format_status
        text = show(dec, ctl, cfg, now, count_agents(sd, now, cfg))
        return {'decision': 'block', 'reason': _reply(cmd, ctl, text)}
```

In `_ctl`, replace the setup branch and the final `print`:

```python
    if cmd in ('setup', 'setup-silent'):
        first_use_menu(cmd, {})
        print(cmd_setup(config_dir(), __file__, cmd == 'setup-silent'))
        return 0
```

```python
    show = format_brief if cmd == 'brief' else format_status
    print(_reply(cmd, ctl, show(dec, ctl, cfg, now, count_agents(sd, now, cfg))))
    return 0
```

- [ ] **Step 5: Run to verify they pass, then the whole file**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -v`
Expected: PASS (all of `test_pacing.py`).

- [ ] **Step 6: Commit**

```bash
git add scripts/pacing.py tests/test_pacing.py
git commit -m "feat: show a one-time template menu on the first /pace (#1)"
```

---

### Task 5: docs, spec amendment and version 0.13

**Files:**
- Modify: `README.md` (Commands table, Install paragraph)
- Modify: `docs/pacing-cadencer-readme.html` (commands table, status line table, version badge)
- Modify: `commands/pace.md` (lines 2-3)
- Modify: `.claude-plugin/plugin.json` (`version`)
- Modify: `scripts/pacing.py` (`VERSION` at line 32)
- Modify: `docs/superpowers/specs/2026-10-15-pace-template-after-design.md` (no-sample tag)
- Test: existing `Versioning` class and `test_install.py` (they read the version from code, manual and plugin)

- [ ] **Step 1: Bump the version everywhere**

`scripts/pacing.py` line 32: `VERSION = '0.13'`. `.claude-plugin/plugin.json` line 3: `"version": "0.13",`. In `docs/pacing-cadencer-readme.html` line 170 replace `<span class="brand__ver">v0.12</span>` with `<span class="brand__ver">v0.13</span>`.

- [ ] **Step 2: Commands file**

`commands/pace.md` line 2: replace `template L1|L2|C1|C2|off)` with `template L1|L2|C1|C2|after|off)`. Line 3: replace `template [L1|L2|C1|C2|off]` with `template [L1|L2|C1|C2|after|off]`.

- [ ] **Step 3: README**

Replace the template row:

```
| `/pace template L1\|L2\|C1\|C2\|after\|off` | choose the status line layout; `after` adds one summary line after your own status line |
```

After the line `Axes start OFF (observe only). ...` add:

```
The first `/pace` after install prints a one-time menu with the three status line choices: a ready layout, keep yours (`/pace template off`), or one summary line after yours (`/pace template after`). `after` needs one change in your script: pass `--after` to `statusline --full` and print what follows a leading `\x02` after your own lines. A script that does not pass `--after` is unaffected.
```

- [ ] **Step 4: Manual**

In `docs/pacing-cadencer-readme.html`, after the `pace template L1|L2|C1|C2|off` row (line 310) insert:

```html
              <tr><td><code>pace template after</code></td><td data-pt="Acrescenta uma linha-resumo depois da última linha do seu script. O script precisa passar <code>--after</code> (ver Ler a statusline).">Adds one summary line after the last line of your script. The script must pass <code>--after</code> (see Reading the status line).</td></tr>
```

In the status line table, after the `r:wk` / `r:off` row (the last `<tr>` before `</tbody>` in `#statusline`) insert:

```html
            <tr><td><code>pace [L1] 5h:31% 44&gt;18%/h cut 59% reset 2h10m | wk:41% 4%/h</code></td><td data-pt="Linha de <code>template after</code>, impressa depois das linhas do seu script: o script passa <code>--after</code> e imprime o que vem depois de <code>\x02</code>. <code>5h:</code> e <code>wk:</code>: % usado, ritmo (<code>atual&gt;necessário</code> só enquanto atrasa), <code>cut</code> = corte, <code>reset</code> só na janela de 5 h; o desvio semanal aparece fora da faixa morta.">Line of <code>template after</code>, printed after your script's own lines: the script passes <code>--after</code> and prints what follows <code>\x02</code>. <code>5h:</code> and <code>wk:</code>: % used, rate (<code>current&gt;needed</code> only while behind), <code>cut</code> = reduction, <code>reset</code> on the 5 h window only; the weekly deviation appears outside the dead band.</td></tr>
```

- [ ] **Step 5: Amend the spec**

In `docs/superpowers/specs/2026-10-15-pace-template-after-design.md` replace the line

```
- No sample (`state` `na`/`stale`): `pace [off] no usage sample yet`.
```

with

```
- No sample (`state` `na`/`stale`): `pace <tag> no usage sample yet`, the tag being the segment's (`[off]` while nothing is engaged, `[n/a]` or `[?]` otherwise).
```

and in the Tests table replace the row `| \`format_line\` no sample | \`pace [off] no usage sample yet\` |` with `| \`format_line\` no sample | \`pace [n/a] no usage sample yet\` when an axis is engaged, \`pace [off] ...\` when none is |`.

- [ ] **Step 6: Verify and commit**

Run: `python -m unittest discover -s tests -p "test_pacing.py" -k Versioning -v`
Expected: PASS (`test_manualBrandTagMatchesTheCode` reads `v0.13`).

```bash
git add README.md docs/pacing-cadencer-readme.html commands/pace.md .claude-plugin/plugin.json scripts/pacing.py docs/superpowers/specs/2026-10-15-pace-template-after-design.md
git commit -m "docs: document template after and the first-use menu; bump to 0.13 (#1)"
```

---

### Task 6: run the whole suite

**Files:** none changed.

- [ ] **Step 1: Run package tests and the install simulator**

Run, from `C:\Users\bruno\claude-pace`: `python -m unittest discover -s tests -v`
Expected: exit code 0. Report the raw `exit_code`, the final `Ran N tests` line, and any `skipped` reason verbatim (a launcher or shell absent on this machine is a skip, never a pass).

- [ ] **Step 2: Confirm nothing leaked into the real profile**

Run: `ls ~/.claude/config/pacing.welcomed`
Expected: `No such file or directory` (the suite wrote its marker only into temporary directories).

---

## Model Routing

| Step | Role | Tier | Model | Rationale |
|---|---|---|---|---|
| 1, 2, 3, 4. state value, summary line, `--after` contract, first-use menu | gen | Standard | Workhorse | test authoring plus judgment on a contract other scripts depend on |
| 5. docs, spec amendment, version bump | gen | Standard | Workhorse | doc and spec prose, bilingual manual |
| 6. run the full suite, report `exit_code` and tail | gen | Mechanical | Executor | running tests; caller checks the raw `exit_code` and the `Ran N tests` line |
| Pre-merge review | review | Architectural | Architect | verifier ≥ generator; contract change (`\x02`) and a persistent marker |
