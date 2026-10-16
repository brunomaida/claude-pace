#!/usr/bin/env python3
"""Pacing cadencer: spread weekly subscription use over business days by adding latency.

Subcommands (stdin = hook / statusLine JSON):
  statusline   statusLine command: record a sample, recompute the decision, print `cadence: ...`;
               `--full` prints the whole status line in the `pace template` layout (L1, L2, C1, C2) from the
               statusLine JSON plus the repo text in `CLAUDE_STATUS_GIT` (one repo per line); while no template is
               chosen it prints the `cadence: ...` segment (the shell keeps its own layout, one python start),
               on a bad payload nothing. The template lives in ctl.json, as volatile as the on/off switches
               `--full --after`: with `pace template after` the reply is `\x02` + one summary line, for a shell that
               prints it after its own lines; without `--after` the reply is the `\x01` segment as before
  enforce      PreToolUse (all tools): sleep the decided delay; at level>=2 cap concurrent subagents
  prompt       UserPromptSubmit: handle `/pace ...`, emit one line on level transitions
  agent-start  SubagentStart: confirm a claimed subagent slot
  agent-done   SubagentStop: release one confirmed subagent slot
  ctl <cmd>    CLI: [wk|5h] on | off | measure [on|off]; up | down | auto | reset | status | guide | template <name>|after|off | setup
               `guide` prints the path of the manual that ships with pace and opens it in the browser
               `setup` writes `statusLine` (a plugin cannot) pointing at a stable copy; `statusline --full --standalone`
               is pace as the whole status line (layout L1 until `pace template` chooses another);
               `setup silent` / `statusline --record-only` sample and print nothing: the status line is unchanged

Two independent axes, weekly (wk) and 5h, each with `measure` (show the rate and the proposed reduction) and
`on` (apply it); the statusline ends with `r:<acting axes>|off`. Default is all OFF: with no ctl file
`enforce` exits at once. Every failure is fail-open (exit 0, no output).
State is volatile (%TEMP%/claude-pacing; claude-pacing-<uid>, mode 0700, on POSIX); samples are kept for the longest window plus `stale_s`, so the
u5 envelope has a fresh reading before the oldest window starts.
Unit: every `%` is an absolute share of the weekly quota (71% used - 38% budget = +33%).
"""
from __future__ import annotations
import datetime, filecmp, glob, json, math, os, pathlib, re, shutil, stat, sys, tempfile, time

# Version of pace itself, not a roadmap phase: every change or request adds 1 to the minor counter (0.1, 0.2, ...
# 0.9, 0.10); the major moves only on an explicit release or macro request. The manual's brand tag must match.
VERSION = '0.15'

DEFAULTS = {
    'ceiling_pct': 95,                  # share of the weekly quota the budget may reach (5% reserve)
    'deadband_pct': 2,                  # lead at or below this = no pacing
    'deadband5_pct': 0.5,               # 5h deficit (% of the 5h window) at or below this = no pacing
    'cap_s': 45,                        # hard ceiling for one delay (cache TTL / hook timeout)
    'k_s_per_pct': 20,                  # seconds of delay per 1% of deficit
    'k5_s_per_pct': 9,                  # seconds of delay per 1% (of the 5h window) of 5h deficit
    'r_pp7_per_pp5': 0.1,               # weekly % consumed per 1% of the 5h window (tune with `ctl status`)
    'windows_min': [5, 10, 15, 25, 30],
    'min_span_s': 60,                   # ignore windows covered by less than this
    'recompute_s': 30,
    'display_window_min': 15,           # window whose rate the statusline shows as the current cadence
    'stale_s': 600,                     # older decision/sample => fail-open
    'cache_ttl_s': 300,                 # shortest prompt-cache TTL to protect (5m tier)
    'cache_margin_s': 90,               # room for generation time the transcript timestamp cannot see
    'deescalate_min': 15,
    'five_hour_block_pct': 95,          # 5h usage that forces level 3 (max delay)
    'n_max': 2,                         # concurrent subagents at level>=2
    'agent_ttl_min': 60,                # active subagent slot lifetime if SubagentStop never fires
    'pending_ttl_s': 30,                # claimed slot not confirmed by SubagentStart (e.g. spawn denied elsewhere)
    'exempt_agents': [],
    'exempt_tools': ['AskUserQuestion', 'ExitPlanMode'],
    'workdays': [0, 1, 2, 3, 4],        # Monday=0
    'hours': [0, 24],                   # active hours inside a business day
    'steps': [0.5, 0.75, 1.0, 1.5, 2.0, 3.0],
}
AXES = ('wk', 'h5')                 # ctl.json keys; `h5` is shown as `5h`
PACE_RE = re.compile(
    r'^\s*/?pace(?:\s+(?:(wk|weekly|5h)\s+(on|off|measure(?:\s+(?:on|off))?)'
    r'|(on|off|up|down|auto|reset|status|brief|guide|setup(?:\s+silent)?|measure(?:\s+(?:on|off))?|template(?:\s+(?:l1|l2|c1|c2|off|after))?)))?\s*$', re.I)
TEMPLATES = ('L1', 'L2', 'C1', 'C2')  # status line layouts chosen with `pace template <name>|off`; none = shell layout
SEGMENT_MARK = '\x01'                 # `statusline --full` prefix that tells the shell "no template, this is the segment"
APPEND_MARK = '\x02'                  # `statusline --full --after` prefix: the rest is one line for after the shell's own
NO_DATA_HINT = 'no usage data: needs Claude Code 2.1.80+, Pro/Max and the pace status line (/pace setup)'
WELCOME_MENU = '\n'.join((
    'pace: first use. Choose how it shows on your status line:',
    '  /pace template L2      ready layout (L1 L2 C1 C2)',
    '  /pace template off     keep your status line as it is',
    '  /pace template after   add one summary line after yours'))
AFTER_NOTE = ('pace template after: call `statusline --full --after` in your script (plain `statusline` ignores '
              'the flag). Its first byte says what follows: \\x02 = one line to print after your own lines, '
              '\\x01 = the cadence segment, anything else = a whole layout to print as it is. '
              'Until then nothing changes.')
# POSIX: one directory per user. A fixed `/tmp/claude-pacing` is shared, and whoever creates it first owns it.
_DEFAULT_STATE = os.path.join(tempfile.gettempdir(),
                              'claude-pacing' if os.name == 'nt' else f'claude-pacing-{os.getuid()}')
STATES = ('na', 'stale', 'ok', 'L1', 'L2', 'L3')  # the only values `decide` writes as `state`
FUTURE_SKEW_S = 60                  # how far ahead of `now` a state file's timestamp may be before it is ignored
_SID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
_DECISION_NUMBERS = ('u5', 'u7', 'r5', 'r7', 'level', 'delay_rec', 'lead', 'allow_day', 'deficit', 'now_rate',
                     'need_rate', 'reduce_pct', 'cap_ticks', 'last_cap_ts', 'delay_wk', 'delay_5h', 'deficit5',
                     'need_rate5', 'now_rate5', 'reduce5_pct', 'mult')  # every numeric field `decide` writes
MAX_CAP_S = 55                      # settings.json PreToolUse timeout is 60 s: a longer sleep would be killed


# ---- config / state io ----------------------------------------------------

def config_dir() -> str:
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')


def load_config() -> dict:
    path = os.environ.get('CLAUDE_PACING_CONFIG') or os.path.join(config_dir(), 'config', 'pacing.config.json')
    cfg = dict(DEFAULTS)
    data = _read_json(path)
    if isinstance(data, dict):
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    # a pause may never exceed the hook timeout nor eat the cache window, whatever the config says
    cfg['cap_s'] = max(0, min(cfg['cap_s'], MAX_CAP_S, cfg['cache_ttl_s'] - cfg['cache_margin_s']))
    return cfg


def _dir_problem(st, uid: int) -> str:
    """Why a state directory may not be trusted ('' when it may): a link or a file, or not the caller's."""
    if not stat.S_ISDIR(st.st_mode):
        return 'not a directory (a link or a file)'
    if st.st_uid != uid:
        return 'owned by another user'
    return ''


def state_dir(create: bool = False) -> str:
    """The state directory. On POSIX it is created 0700 even for a reader and must be the caller's own real
    directory: anything else raises PermissionError, which every caller turns into fail-open. A loose mode is
    tightened on the default directory only; one named by CLAUDE_PACING_DIR is the user's to manage."""
    override = os.environ.get('CLAUDE_PACING_DIR')
    d = override or _DEFAULT_STATE
    posix = os.name != 'nt'
    if create or posix:
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except FileExistsError:
            pass  # a file or a dangling link where the directory should be: refused just below
    if posix:
        st = os.lstat(d)
        why = _dir_problem(st, os.getuid())
        if why:
            raise PermissionError(f'unsafe state dir {d}: {why}')
        if st.st_mode & 0o077 and not override:
            os.chmod(d, 0o700)
    return d


def _read_json(path: str):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_text(path: str, text: str, mode: int | None = None) -> bool:
    """Atomic write through an exclusive 0600 temp file, so a planted name or link is never followed; `mode`,
    when given, is applied to it before it replaces `path`. False when the file stayed locked (state writers
    ignore it, `setup` reports it)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', prefix=os.path.basename(path) + '.',
                               suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        if mode is not None and os.name != 'nt':
            os.chmod(tmp, mode)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.02)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def _write_json(path: str, obj) -> None:
    _write_text(path, json.dumps(obj))


def _safe_sid(value) -> str:
    """A session or agent id as a file-name part: a plain token, else `x` (never a path)."""
    s = str(value or '')
    return s if _SID_RE.fullmatch(s) else 'x'


def _read_decision(sd: str, now: float):
    """decision.json, or None when it is missing, dated in the future or carries a state that `decide` never
    writes. What reaches a status line or a reply is checked here, because the file sits in a directory that
    another process could have written. Rate tables with a non-numeric key are dropped whole."""
    dec = _read_json(os.path.join(sd, 'decision.json'))
    if not isinstance(dec, dict):
        return None
    ts = dec.get('ts')
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts > now + FUTURE_SKEW_S:
        return None
    if dec.get('state', 'na') not in STATES:
        return None
    for key in _DECISION_NUMBERS:  # printed raw by `format_status`: a string here could carry escape codes
        v = dec.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
            return None
    for key in ('rates', 'rates5'):
        table = dec.get(key)
        if table is None:
            continue
        try:
            pairs = [(float(k), float(v)) for k, v in table.items()]
            if not all(math.isfinite(k) and math.isfinite(v) for k, v in pairs):
                raise ValueError(key)
            dec[key] = {f'{k:g}': v for k, v in pairs}  # a window key is re-rendered, never kept raw
        except (AttributeError, TypeError, ValueError):
            del dec[key]
    return dec


def read_ctl(sd: str) -> dict:
    ctl = _read_json(os.path.join(sd, 'ctl.json'))
    return ctl if isinstance(ctl, dict) else {}


# ---- business time --------------------------------------------------------

def biz_seconds(a: float, b: float, off: float, workdays, h0: float, h1: float) -> float:
    """Seconds of [a, b] that fall on workdays inside [h0, h1) hours; `off` = local UTC offset (s)."""
    if b <= a:
        return 0.0
    la, lb = a + off, b + off
    total, day = 0.0, int(la // 86400)
    while day <= int(lb // 86400):
        if (day + 3) % 7 in workdays:  # 1970-01-01 was a Thursday (Monday=0)
            s, e = day * 86400 + h0 * 3600, day * 86400 + h1 * 3600
            total += max(0.0, min(e, lb) - max(s, la))
        day += 1
    return total


def local_offset(now: float) -> float:
    return -(time.altzone if time.localtime(now).tm_isdst > 0 else time.timezone)


# ---- controller -----------------------------------------------------------

def default_step(cfg: dict) -> int:
    steps = cfg['steps']
    return steps.index(1.0) if 1.0 in steps else len(steps) // 2


def is_manual(ctl: dict, cfg: dict) -> bool:
    """`up`/`down` left the suggested step; `auto` (or `reset`) puts it back."""
    return ctl.get('step', default_step(cfg)) != default_step(cfg)


def decide(now: float, samples: list, cfg: dict, prev: dict | None = None, off: float = 0.0,
           axes: tuple = ('wk',), mult: float = 1.0) -> dict:
    prev = prev or {}
    if 'axes' in prev and prev['axes'] != list(axes):
        prev = {}
    dec = {'ts': now, 'state': 'na', 'level': 0, 'delay_rec': 0.0}
    if not samples:
        return dec
    # an idle session can report old rate_limits with a new ts: trust the newest window/usage, not the newest ts
    last = max(samples, key=lambda x: (x.get('r7') or 0, x.get('r5') or 0, x.get('u7') or 0, x.get('u5') or 0, x['ts']))
    u5, u7, r5, r7 = (last.get(k) for k in ('u5', 'u7', 'r5', 'r7'))
    if u7 is None or r7 is None:
        return dec
    if r5 is not None and r5 <= now:  # the 5h window already reset: that reading says nothing about the new one
        u5 = r5 = None
    dec.update(u5=u5, u7=u7, r5=r5, r7=r7)
    if now - last['ts'] > cfg['stale_s'] or r7 <= now:  # an expired week has no budget to measure against
        dec['state'] = 'stale'
        return dec

    h0, h1 = cfg['hours']
    wd, ceiling = set(cfg['workdays']), cfg['ceiling_pct']
    start = r7 - 7 * 86400
    total = biz_seconds(start, r7, off, wd, h0, h1)
    elapsed = biz_seconds(start, min(now, r7), off, wd, h0, h1)
    remaining = biz_seconds(max(now, start), r7, off, wd, h0, h1)
    daily = (h1 - h0) * 3600
    lead = u7 - (ceiling * elapsed / total if total > 0 else ceiling)
    allow_day = max(0.0, ceiling - u7) / (remaining / daily) if remaining > 0 else None

    need = allow_day / daily if allow_day is not None else None  # weekly % per second
    need5 = None  # 5h-% per hour that still ends on the ceiling when the window resets
    if u5 is not None and r5 is not None:
        need5 = max(0.0, cfg['five_hour_block_pct'] - u5) / (max(r5 - now, cfg['min_span_s']) / 3600)
    deficit, deficit5, trend, rates, rates5 = 0.0, 0.0, {}, {}, {}
    # u5 never falls inside one r5 window, so a lower reading is an idle session's stale copy: the running
    # max over every session's samples is the account's true level at each point in time
    chain, top = [], float('-inf')
    for s in samples:
        if r5 is not None and s.get('r5') == r5 and s.get('u5') is not None:
            top = max(top, s['u5'])
            chain.append((s['ts'], top))
    for w in cfg['windows_min']:
        pts = [c for c in chain if c[0] >= now - w * 60]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < cfg['min_span_s']:
            continue
        d5 = pts[-1][1] - pts[0][1]
        hours = (pts[-1][0] - pts[0][0]) / 3600
        trend[w], rates5[w] = d5, d5 / hours
        if need is not None:
            rates[w] = d5 * cfg['r_pp7_per_pp5'] / hours  # weekly %/h
            deficit = max(deficit, d5 * cfg['r_pp7_per_pp5'] - need * (now - pts[0][0]))
        if need5 is not None:
            deficit5 = max(deficit5, d5 - need5 / 3600 * (now - pts[0][0]))
    deficit, deficit5 = max(deficit, 0.0), max(deficit5, 0.0)  # `cap_s` bounds the delay: no clamp needed

    cap = float(cfg['cap_s'])
    wk_on, h5_on = 'wk' in axes, 'h5' in axes
    delay_wk = min(cap, deficit * cfg['k_s_per_pct']) if lead > cfg['deadband_pct'] else 0.0
    band5 = 0.0 if prev.get('level', 0) >= 1 else cfg['deadband5_pct']  # enter above the band, leave at zero
    delay_5h = min(cap, deficit5 * cfg['k5_s_per_pct']) if deficit5 > band5 else 0.0
    if u5 is not None and u5 >= cfg['five_hour_block_pct']:
        level, delay_wk, delay_5h = 3, cap, cap
    elif (wk_on and lead > cfg['deadband_pct']) or (h5_on and deficit5 > band5):
        level = 1
    else:
        level = 0
    delay = max(delay_wk if wk_on else 0.0, delay_5h if h5_on else 0.0)
    if level == 3:
        delay = cap

    at_cap = level >= 1 and delay * mult >= cap - 1e-9  # the delay `enforce` really sleeps
    cap_ticks = prev.get('cap_ticks', 0) + 1 if at_cap else 0
    last_cap = now if at_cap else prev.get('last_cap_ts')
    sticky = prev.get('level') == 2 and last_cap and now - last_cap < cfg['deescalate_min'] * 60
    if level == 1 and (cap_ticks >= 2 or sticky):
        level = 2
    elif level == 0 and sticky:  # pressure cleared: keep the subagent cap until the de-escalation window ends
        level = 2

    def shown(rs: dict):
        got = rs.get(cfg['display_window_min'])
        return got if (got is not None or not rs) else rs[max(rs)]
    now_rate, now_rate5 = shown(rates), shown(rates5)
    need_rate = allow_day / (daily / 3600) if allow_day is not None else None
    reduce_pct = round(100 * max(0.0, 1 - need_rate / now_rate)) if now_rate and need_rate is not None else 0
    reduce5_pct = round(100 * max(0.0, 1 - need5 / now_rate5)) if now_rate5 and need5 is not None else 0
    dec.update(state=('ok', 'L1', 'L2', 'L3')[level], level=level, delay_rec=delay, lead=lead,
               allow_day=allow_day, deficit=deficit, trend=trend, rates=rates, now_rate=now_rate,
               need_rate=need_rate, reduce_pct=reduce_pct, cap_ticks=cap_ticks, last_cap_ts=last_cap,
               delay_wk=delay_wk, delay_5h=delay_5h, deficit5=deficit5, need_rate5=need5,
               now_rate5=now_rate5, rates5=rates5, reduce5_pct=reduce5_pct, axes=list(axes), mult=mult)
    return dec


def step_mult(ctl: dict, cfg: dict) -> float:
    """Multiplier of the chosen step (`up`/`down`) applied to the recommended delay."""
    step = norm_ctl(ctl).get('step', default_step(cfg))
    return cfg['steps'][min(max(step, 0), len(cfg['steps']) - 1)]


def effective_delay(dec: dict, ctl: dict, cfg: dict) -> float:
    acting = acting_axes(ctl)
    if not acting:
        return 0.0
    recs = {'wk': dec.get('delay_wk', dec.get('delay_rec', 0.0)), 'h5': dec.get('delay_5h', 0.0)}
    return min(float(cfg['cap_s']), max(recs[a] for a in acting) * step_mult(ctl, cfg))


def norm_ctl(ctl: dict) -> dict:
    """ctl with per-axis `{on, measure}` bools; legacy flat `on`/`measure` are read as the weekly axis."""
    out = dict(ctl)
    legacy = {k: out.pop(k) for k in ('on', 'measure') if k in out}
    for axis in AXES:
        src = dict(legacy) if axis == 'wk' else {}
        src.update(out.get(axis) or {})
        out[axis] = {'on': bool(src.get('on')), 'measure': bool(src.get('measure'))}
    return out


def acting_axes(ctl: dict) -> list:
    c = norm_ctl(ctl)
    return [a for a in AXES if c[a]['on']]


def engaged_axes(ctl: dict) -> list:
    c = norm_ctl(ctl)
    return [a for a in AXES if c[a]['on'] or c[a]['measure']]


def drive_axes(ctl: dict) -> tuple:
    """Axes `decide` computes level and delay for: acting ones win so a measuring axis never escalates enforcement."""
    return tuple(acting_axes(ctl) or engaged_axes(ctl)) or ('wk',)


def fmt_rate(v: float) -> str:
    """Rate in the segment: integer from 1 up, one decimal below (decided on the 1-decimal rounding)."""
    r = round(v, 1)
    return str(int(r + 0.5)) if r >= 1 else f'{r:.1f}'


def apply_ctl(cmd: str, ctl: dict, cfg: dict) -> dict:
    ctl = norm_ctl(ctl)
    ctl.setdefault('step', default_step(cfg))
    top = len(cfg['steps']) - 1
    axis, _, verb = cmd.rpartition(':')
    axes = (axis,) if axis else AXES
    if verb in ('on', 'off'):
        for a in axes:
            ctl[a]['on'] = verb == 'on'
    elif verb in ('measure-on', 'measure-off'):
        for a in axes:
            ctl[a]['measure'] = verb == 'measure-on'
    elif verb == 'up':
        ctl['step'] = min(top, ctl['step'] + 1)
    elif verb == 'down':
        ctl['step'] = max(0, ctl['step'] - 1)
    elif verb == 'auto':
        ctl['step'] = default_step(cfg)
    elif verb == 'reset':
        for a in AXES:
            ctl[a]['on'] = True
        ctl['step'] = default_step(cfg)
    elif verb.startswith('tpl-'):
        ctl['template'] = verb[4:].upper()
    return ctl


def parse_pace_prompt(prompt: str):
    m = PACE_RE.match(prompt or '')
    if not m:
        return None
    axis, verb = m.group(1), m.group(2) or m.group(3)
    word = ' '.join((verb or 'status').lower().split())
    if word.startswith('measure'):
        word = 'measure-off' if word.endswith('off') else 'measure-on'
    elif word.startswith('template'):
        word = 'tpl-' + word.split()[1] if ' ' in word else 'status'
    elif word == 'setup silent':
        word = 'setup-silent'
    return f'{"h5" if axis.lower() == "5h" else "wk"}:{word}' if axis else word


# ---- rendering ------------------------------------------------------------

def render_segment(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    c = norm_ctl(ctl)
    acting, engaged = acting_axes(c), engaged_axes(c)
    red = 'r:' + ('+'.join('5h' if a == 'h5' else a for a in acting) or 'off')
    state = dec.get('state')
    lead = dec.get('lead', 0.0)
    lead_tok = f'{lead:+.0f}%' if abs(lead) > cfg['deadband_pct'] else ''
    if not engaged:  # observe-only default: lead is the one cheap signal
        if state in ('na', 'stale'):
            return 'cadence: [off]'
        return 'cadence: ' + ' '.join(p for p in ('[off]', lead_tok) if p)
    if state in ('na', 'stale'):
        return f'cadence: [{"n/a" if state == "na" else "?"}] {red}'
    level = dec['level']
    parts = [f'[{state}]']
    if level != 3:
        blocks = []
        if 'wk' in engaged:
            items = [lead_tok]
            now_rate, need_rate = dec.get('now_rate'), dec.get('need_rate')
            if level >= 1 and lead > cfg['deadband_pct'] and now_rate is not None and need_rate is not None:
                items.append(f'{fmt_rate(now_rate)}>{fmt_rate(need_rate)}%/h')
                if dec.get('reduce_pct', 0) > 0:
                    items.append(f'-{dec["reduce_pct"]}%')
            elif now_rate is not None:
                items.append(f'{fmt_rate(now_rate)}%/h')
            if any(items):
                blocks.append('week:' + ' '.join(i for i in items if i))
        if 'h5' in engaged:
            now5, need5, items = dec.get('now_rate5'), dec.get('need_rate5'), []
            if now5 is not None and need5 is not None and dec.get('deficit5', 0) > cfg['deadband5_pct']:
                items.append(f'{fmt_rate(now5)}>{fmt_rate(need5)}%/h')
                if dec.get('reduce5_pct', 0) > 0:
                    items.append(f'-{dec["reduce5_pct"]}%')
            elif now5 is not None:
                items.append(f'{fmt_rate(now5)}%/h')
            if items:
                blocks.append('5h:' + ' '.join(items))
        if blocks:
            parts.append(' . '.join(blocks))
    cap = cfg['cap_s']
    mult = cfg['steps'][min(max(c.get('step', default_step(cfg)), 0), len(cfg['steps']) - 1)]
    delay = effective_delay(dec, c, cfg) if acting else min(float(cap), dec.get('delay_rec', 0.0))
    if delay > 0:
        parts.append(('' if acting else '~') + f'{delay:.0f}s' + ('!' if delay >= cap else ''))
    if acting and is_manual(c, cfg):
        parts.append(f'manual x{mult:g}')
    if level == 3:
        parts.append(f'5h:{max(0, int(((dec.get("r5") or now) - now) // 60))}m')
    if level == 2 and acting:
        parts.append(f'{active}/{cfg["n_max"]}')
        if dec.get('allow_day') is not None:
            parts.append(f'{fmt_rate(dec["allow_day"])}%/d')
    parts.append(red)
    return 'cadence: ' + ' '.join(p for p in parts if p)


def format_status(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    c = norm_ctl(ctl)
    mult = cfg['steps'][min(max(c.get('step', default_step(cfg)), 0), len(cfg['steps']) - 1)]
    sw = lambda a, name: f'{name} reduction {"on" if c[a]["on"] else "off"} measure {"on" if c[a]["measure"] else "off"}'
    bits = [sw('wk', 'wk'), sw('h5', '5h'), render_segment(dec, c, cfg, now, active),
            f'template {template_of(c) or ("after" if after_of(c) else "off")}',
            f'mode {"manual" if is_manual(c, cfg) else "auto"}',
            f'step x{mult:g}', f'r={cfg["r_pp7_per_pp5"]}', f'pace v{VERSION}']
    if dec.get('state') == 'na':
        bits.append(NO_DATA_HINT)
    if dec.get('allow_day') is not None:
        bits.append(f'allow {dec["allow_day"]:.1f}%/day')
    if dec.get('need_rate') is not None:
        bits.append(f'need {dec["need_rate"]:.2f}%/h')
    if dec.get('rates'):
        bits.append('rate %/h ' + ' '.join(f'{w}m:{v:.2f}' for w, v in sorted(dec['rates'].items())))
        bits.append(f'proposed -{dec.get("reduce_pct", 0)}% (~{dec.get("delay_wk", dec.get("delay_rec", 0)):.0f}s)')
    if dec.get('need_rate5') is not None:
        bits.append(f'need5 {dec["need_rate5"]:.2f}%/h')
    if dec.get('rates5'):
        bits.append('rate5 %/h ' + ' '.join(f'{w}m:{v:.2f}' for w, v in sorted(dec['rates5'].items())))
        bits.append(f'proposed5 -{dec.get("reduce5_pct", 0)}% (~{dec.get("delay_5h", 0):.0f}s)')
    return ' | '.join(bits)


def _r1(v: float) -> str:
    """Rate for the brief: one decimal below 10 (weekly rates sit around 1-2), integer from 10 up."""
    return f'{v:.1f}' if v < 10 else f'{v:.0f}'


def format_brief(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    """Condensed status for narrow clients: a header, then one line per observed axis (5h first)."""
    c = norm_ctl(ctl)
    f = cadence_fields(dec, c, cfg, now, active)
    mult = cfg['steps'][min(max(c.get('step', default_step(cfg)), 0), len(cfg['steps']) - 1)]
    head = ['pace ' + f['tag'], f'{"manual" if is_manual(c, cfg) else "auto"} x{mult:g}']
    if f['delay']:
        head.append('delay ' + f['delay'])
    if f['red']:
        head.append(f['red'])
    lines = [' \u00b7 '.join(head)]
    if dec.get('state') in ('na', 'stale'):
        return '\n'.join(lines + (['no usage sample yet', NO_DATA_HINT] if engaged_axes(c) else []))
    band = cfg['deadband_pct']
    for ax, label, used, now_k, need_k, cut_k in (
            ('h5', '5h', 'u5', 'now_rate5', 'need_rate5', 'reduce5_pct'),
            ('wk', 'week', 'u7', 'now_rate', 'need_rate', 'reduce_pct')):
        if not (c[ax]['on'] or c[ax]['measure']):
            continue
        parts = []
        if dec.get(used) is not None:
            parts.append(f'{dec[used]:.0f}% used')
        if dec.get(now_k) is not None:
            of = f' of {_r1(dec[need_k])}' if dec.get(need_k) is not None else ''
            parts.append(f'{_r1(dec[now_k])}{of} %/h')
        if dec.get(cut_k, 0) > 0:
            parts.append(f'cut {dec[cut_k]}%')
        if ax == 'h5' and dec.get('r5'):
            parts.append('resets ' + fmt_left(max(0, dec['r5'] - now)))
        if ax == 'wk' and dec.get('lead') is not None:
            lead = dec['lead']
            parts.append('on budget' if abs(lead) <= band else
                         f'{lead:.0f}% over budget' if lead > 0 else f'{-lead:.0f}% under budget')
        if parts:
            lines.append(f'{label:<5} ' + ' \u00b7 '.join(parts))
    return '\n'.join(lines)


def format_line(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    """One line for `template after`: the state tag, then a 5h and a week block for the observed axes."""
    try:
        c = norm_ctl(ctl)
        f = cadence_fields(dec, c, cfg, now, active)
        head = 'pace ' + f['tag']
        if dec.get('state') in ('na', 'stale'):
            return head + ' no usage sample yet'
        blocks = []
        full5h = dec.get('level') == 3 and bool(engaged_axes(c))  # the cap is the 5h window's: say when it ends
        for ax, label, used_k, reset_k in (('h5', '5h', 'u5', 'r5'), ('wk', 'wk', 'u7', None)):
            if not (c[ax]['on'] or c[ax]['measure'] or (ax == 'h5' and full5h)):
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


# ---- status line templates ------------------------------------------------

def template_of(ctl: dict) -> str:
    """The chosen layout, or '' (none): the status line script then keeps its own three lines."""
    t = str(ctl.get('template') or '').upper()
    return t if t in TEMPLATES else ''


def after_of(ctl: dict) -> bool:
    """`pace template after`: the summary line goes after the shell's own lines instead of a layout."""
    return str(ctl.get('template') or '').upper() == 'AFTER'


def bar(pct: float) -> str:
    n = min(10, max(0, int(pct / 10 + 0.5)))
    return '▓' * n + '░' * (10 - n)


def fmt_left(secs: float, compact: bool = False) -> str:
    """Time to a reset: `3d 4h`, `2h 10m`, `45m`, `30s` (compact drops the space)."""
    s = int(secs)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    parts = (f'{d}d', f'{h}h') if d else (f'{h}h', f'{m}m') if h else (f'{m}m',) if m else (f'{s}s',)
    return ('' if compact else ' ').join(parts)


def _pace(now_rate, need_rate, behind: bool, reduce_pct) -> tuple | None:
    """`(rate, reduction)` as in render_segment: `now>need` and `-N%` only while behind the target."""
    if now_rate is None:
        return None
    if behind and need_rate is not None:
        return f'{fmt_rate(now_rate)}>{fmt_rate(need_rate)}', (f'-{reduce_pct}%' if reduce_pct > 0 else '')
    return fmt_rate(now_rate), ''


def cadence_fields(dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> dict:
    """The parts of render_segment that a template places itself; `red` is empty while nothing is engaged."""
    c = norm_ctl(ctl)
    acting, engaged = acting_axes(c), engaged_axes(c)
    state, lead, band = dec.get('state'), dec.get('lead', 0.0), cfg['deadband_pct']
    pool = acting or engaged
    if len(pool) > 1:  # both: the axis whose recommendation is larger drives the delay (ties go to 5h)
        axis = 'wk' if dec.get('delay_wk', dec.get('delay_rec', 0.0)) > dec.get('delay_5h', 0.0) else 'h5'
    else:
        axis = pool[0] if pool else 'wk'
    f = {'tag': '[off]', 'red': '', 'lead': '', 'wk': None, 'h5': None, 'delay': '', 'extras': [], 'allow': '',
         'axis': axis}  # axis = the one that owns delay and extras
    if not engaged:
        if state not in ('na', 'stale') and abs(lead) > band:
            f['lead'] = f'{lead:+.0f}%'
        return f
    f['red'] = 'r:' + ('+'.join('5h' if a == 'h5' else a for a in acting) or 'off')
    if state in ('na', 'stale'):
        f['tag'] = '[n/a]' if state == 'na' else '[?]'
        return f
    level = dec['level']
    f['tag'] = f'[{state}]'
    if abs(lead) > band:
        f['lead'] = f'{lead:+.0f}%'
    if level != 3:
        if 'wk' in engaged:
            f['wk'] = _pace(dec.get('now_rate'), dec.get('need_rate'), level >= 1 and lead > band, dec.get('reduce_pct', 0))
        if 'h5' in engaged:
            f['h5'] = _pace(dec.get('now_rate5'), dec.get('need_rate5'), dec.get('deficit5', 0) > cfg['deadband5_pct'], dec.get('reduce5_pct', 0))
    cap = cfg['cap_s']
    mult = cfg['steps'][min(max(c.get('step', default_step(cfg)), 0), len(cfg['steps']) - 1)]
    delay = effective_delay(dec, c, cfg) if acting else min(float(cap), dec.get('delay_rec', 0.0))
    if delay > 0:
        f['delay'] = ('' if acting else '~') + f'{delay:.0f}s' + ('!' if delay >= cap else '')
    if acting and is_manual(c, cfg):
        f['extras'].append(f'manual x{mult:g}')
    if level == 2 and acting:
        f['extras'].append(f'{active}/{cfg["n_max"]}')
        if dec.get('allow_day') is not None:
            f['allow'] = f'{fmt_rate(dec["allow_day"])}%/d'
    return f


def view_from_raw(raw: dict, git: str, now: float) -> dict:
    """What the statusLine JSON gives a template: model, context %, git text and the two usage windows."""
    name = re.sub(r'^Claude ', '', str((raw.get('model') or {}).get('display_name') or (raw.get('model') or {}).get('id')
                                       or 'unknown'))
    effort = str((raw.get('effort') or {}).get('level') or '')
    if effort:
        name += f' ({effort[:1].upper()}{effort[1:]})'
    rl = raw.get('rate_limits') or {}

    def window(key: str):
        w = rl.get(key) or {}
        if w.get('used_percentage') is None:
            return None
        left = (w.get('resets_at') or now) - now
        if w.get('resets_at') and left <= 0:  # reset since the session's last request: the window is empty again
            return 0.0, 0
        return w['used_percentage'], max(0, int(left))

    return {'model': name, 'ctx': (raw.get('context_window') or {}).get('used_percentage'), 'git': git,
            'five': window('five_hour'), 'week': window('seven_day')}


def _signed(lead: str, tail: str) -> str:
    """`+8%` -> `+8% over`, `-6%` -> `6% under` (tail is appended to both)."""
    if not lead:
        return ''
    return f'{lead} over{tail}' if lead[0] == '+' else f'{lead[1:]} under{tail}'


def _pace_toks(p) -> list:
    return [x for x in (f'{p[0]}%/h', p[1]) if x] if p else []


def _repos(v: dict) -> list:
    return [s for s in str(v.get('git') or '').split('\n') if s]


def _own(f: dict, ax: str, prefix: str = '') -> list:
    """Delay and its modifiers, for the axis that owns them (the one driving the delay)."""
    return (([prefix + f['delay']] if f['delay'] else []) + f['extras']) if f['axis'] == ax else []


def _tpl_l1(v: dict, f: dict) -> str:
    segs = ([f'context {v["ctx"]:.0f}%'] if v.get('ctx') is not None else []) + _repos(v)
    parts = []
    for label, key in (('5h', 'five'), ('week', 'week')):
        w = v.get(key)
        if w:
            parts.append(f'{label} {bar(w[0])} {w[0]:.0f}%' + (f' ({fmt_left(w[1])})' if w[1] > 0 else ''))
    cad = ' '.join(x for x in (f['tag'], _signed(f['lead'], '')) if x)
    tail = ([f'delay {f["delay"]}'] if f['delay'] else []) + f['extras'] + ([f['allow']] if f['allow'] else [])
    if tail:
        cad += '  ' + ' '.join(tail)
    if f['red'] and not f['delay']:
        cad += ' ' + f['red']  # without a delay, `r:` is the only sign of measure-only vs applied
    return v['model'] + (' |  ' + '  |  '.join(segs) if segs else '') + '\n' + '  |  '.join(parts + [cad])


def _tpl_l2(v: dict, f: dict) -> str:
    ctx = [f'context {v["ctx"]:.0f}% '] if v.get('ctx') is not None else []
    cad = 'cadence ' + ' '.join(x for x in (f['tag'], f['red']) if x)
    rows = []  # label, usage, pace, reduction, extra
    for label, key, ax in (('5h', 'five', 'h5'), ('week', 'week', 'wk')):
        w = v.get(key)
        pace, red = f[ax] or ('', '')
        extra = ' '.join(x for x in ((_signed(f['lead'], ' budget') if ax == 'wk' else ''), *_own(f, ax, 'delay '),
                                     (f['allow'] if ax == 'wk' else '')) if x)
        if not (w or pace or extra):
            continue  # a row without its window still carries the cadence columns
        usage = f'{bar(w[0])} {w[0]:.0f}%' + (f' ({fmt_left(w[1])} left)' if w[1] > 0 else '') if w else ''
        rows.append([label, usage, f'pace {pace} %/h' if pace else '', red, extra])
    widths = [max(len(r[i]) for r in rows) for i in range(5)] if rows else []
    gaps = (0, 3, 2, 3, 0)
    lines = [' | '.join(x for x in (v['model'], *ctx, cad, *_repos(v)) if x)]
    for r in rows:
        s = r[0].ljust(6) + ''.join(r[i].ljust(widths[i]) + ' ' * gaps[i] for i in range(1, 5) if widths[i])
        lines.append(s.rstrip())
    return '\n'.join(lines)


def _tpl_c1(v: dict, f: dict) -> str:
    head = ' | '.join(x for x in (v['model'], f'ctx {v["ctx"]:.0f}%' if v.get('ctx') is not None else '',
                                  *_repos(v)) if x)
    groups = []
    for label, key in (('5h', 'five'), ('wk', 'week')):
        w = v.get(key)
        if w:
            groups.append(f'{label} {w[0]:.0f}%' + (f' {fmt_left(w[1], True)}' if w[1] > 0 else ''))
    cad = ' '.join(x for x in (f['tag'], f['lead'], ' '.join([f['delay'], *f['extras']]).strip(), f['allow'],
                               f['red']) if x)
    return head + '\n' + (' . '.join(groups) + ' > ' + cad if groups else cad)


def _tpl_c2(v: dict, f: dict) -> str:
    lines = [' | '.join(x for x in (v['model'], *_repos(v)) if x)]
    use = [f'ctx {v["ctx"]:.0f}%'] if v.get('ctx') is not None else []
    for label, key in (('5h', 'five'), ('week', 'week')):
        w = v.get(key)
        if w:
            use.append(f'{label} {w[0]:.0f}%' + (f' ({fmt_left(w[1], True)})' if w[1] > 0 else ''))
    if use:
        lines.append('> ' + ' | '.join(use))
    g5 = _pace_toks(f['h5']) + _own(f, 'h5')
    gw = ([f['lead']] if f['lead'] else []) + _pace_toks(f['wk']) + _own(f, 'wk') + ([f['allow']] if f['allow'] else [])
    groups = [f'{lab} ' + ' '.join(g) for lab, g in (('5h', g5), ('wk', gw)) if g]
    line = ' | '.join([' '.join([f['tag'], *groups[:1]]), *groups[1:]])
    lines.append('> ' + line + (' ' + f['red'] if f['red'] and not f['delay'] else ''))  # `r:` only where delay is silent
    return '\n'.join(lines)


def render_template(name: str, view: dict, dec: dict, ctl: dict, cfg: dict, now: float, active: int = 0) -> str:
    f = cadence_fields(dec, ctl, cfg, now, active)
    return {'L1': _tpl_l1, 'L2': _tpl_l2, 'C1': _tpl_c1, 'C2': _tpl_c2}[name](view, f)


# ---- samples / agents -----------------------------------------------------

def retention_s(cfg: dict) -> float:
    """How long samples are kept: the longest window plus a margin, so its oldest point has an earlier seed."""
    return max(cfg['windows_min']) * 60 + cfg['stale_s']


def record_sample(sd: str, sid: str, now: float, u5, u7, r5, r7, cfg: dict) -> None:
    path = os.path.join(sd, f'samples.{sid}.jsonl')
    horizon = now - retention_s(cfg)
    keep = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if s.get('ts', 0) >= horizon:
                    keep.append(s)
    except OSError:
        pass
    if keep and (keep[-1]['u5'], keep[-1]['u7'], keep[-1].get('r5'), keep[-1].get('r7')) == (u5, u7, r5, r7) \
            and now - keep[-1]['ts'] < 60:
        return
    keep.append({'ts': now, 'u5': u5, 'u7': u7, 'r5': r5, 'r7': r7})
    _write_text(path, '\n'.join(json.dumps(s) for s in keep) + '\n')


def load_samples(sd: str, now: float, cfg: dict) -> list:
    horizon = now - retention_s(cfg)
    out = []
    for path in glob.glob(os.path.join(sd, 'samples.*.jsonl')):
        try:
            with open(path, encoding='utf-8') as f:
                rows = [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            continue
        fresh = [r for r in rows if r.get('ts', 0) >= horizon]
        if not fresh:
            try:
                os.remove(path)
            except OSError:
                pass
        out.extend(fresh)
    return sorted(out, key=lambda s: s['ts'])


def _agents_dir(sd: str) -> str:
    return os.path.join(sd, 'agents')


def _lock(sd: str):
    path = os.path.join(sd, 'agents.lock')
    for _ in range(100):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 5:
                    os.remove(path)
            except OSError:
                pass
            time.sleep(0.02)
        except OSError:
            return None
    return None


def _unlock(path) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _slots(sd: str, now: float, cfg: dict) -> list:
    """Live slot files sorted oldest first; `.p` = claimed at PreToolUse, `.a` = confirmed by SubagentStart."""
    live = []
    for p in sorted(glob.glob(os.path.join(_agents_dir(sd), '*'))):
        ttl = cfg['pending_ttl_s'] if p.endswith('.p') else cfg['agent_ttl_min'] * 60
        try:
            if -FUTURE_SKEW_S <= now - os.path.getmtime(p) <= ttl:  # a future-dated slot would never expire
                live.append(p)
            else:
                os.remove(p)
        except OSError:
            pass
    return live


def count_agents(sd: str, now: float, cfg: dict) -> int:
    return len(_slots(sd, now, cfg))


def claim_agent(sd: str, now: float, cfg: dict, cap: bool) -> bool:
    """Atomically count + claim a slot; False only when `cap` and n_max slots are live."""
    os.makedirs(_agents_dir(sd), exist_ok=True)
    lock = _lock(sd)
    if lock is None:
        return True  # fail-open
    try:
        if cap and len(_slots(sd, now, cfg)) >= cfg['n_max']:
            return False
        with open(os.path.join(_agents_dir(sd), f'{time.time_ns()}_{os.getpid()}_{os.urandom(2).hex()}.p'), 'w'):
            pass
        return True
    finally:
        _unlock(lock)


def agent_started(sd: str, cfg: dict, agent_type: str) -> None:
    if agent_type in cfg['exempt_agents']:
        return
    lock = _lock(sd)
    try:
        for p in sorted(glob.glob(os.path.join(_agents_dir(sd), '*.p'))):
            try:
                os.replace(p, p[:-2] + '.a')
                break
            except OSError:
                continue
    finally:
        _unlock(lock)


def agent_stopped(sd: str, cfg: dict, agent_type: str) -> None:
    if agent_type in cfg['exempt_agents']:
        return
    lock = _lock(sd)
    try:
        for p in sorted(glob.glob(os.path.join(_agents_dir(sd), '*.a'))):
            try:
                os.remove(p)
                break
            except OSError:
                continue
    finally:
        _unlock(lock)


def prune_markers(sd: str, now: float) -> None:
    for p in glob.glob(os.path.join(sd, 'slept.*')):
        try:
            if now - os.path.getmtime(p) > 3600:
                os.remove(p)
        except OSError:
            pass


# ---- subcommands ----------------------------------------------------------

def _deny(reason: str) -> dict:
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def _window_turned(prev: dict, five_r5, seven_r7, now: float) -> bool:
    """The cached decision belongs to a window that already reset, or a reading just opened a newer one."""
    for key, seen in (('r5', five_r5), ('r7', seven_r7)):
        old = prev.get(key)
        if old is not None and prev.get('ts', 0) < old <= now:  # expired since that decision was made
            return True
        if seen is not None and seen > now and seen > (old or 0):
            return True
    return False


def _statusline_state(raw: dict, cfg: dict, now: float) -> tuple:
    """Record the sample, recompute the decision when due; returns `(decision, ctl, active subagents)`."""
    sd = state_dir(create=True)
    rl = raw.get('rate_limits') or {}
    five, seven = rl.get('five_hour') or {}, rl.get('seven_day') or {}
    if seven.get('used_percentage') is not None and seven.get('resets_at') is not None:
        record_sample(sd, _safe_sid(raw.get('session_id')), now, five.get('used_percentage'),
                      seven['used_percentage'], five.get('resets_at'), seven['resets_at'], cfg)
    prune_markers(sd, now)
    prev = _read_decision(sd, now)
    ctl = read_ctl(sd)
    axes, mult = drive_axes(ctl), step_mult(ctl, cfg)
    if isinstance(prev, dict) and now - prev.get('ts', 0) < cfg['recompute_s'] and prev.get('axes') == list(axes) \
            and prev.get('mult', 1.0) == mult and not _window_turned(prev, five.get('resets_at'),
                                                                     seven.get('resets_at'), now):
        dec = prev
    else:
        dec = decide(now, load_samples(sd, now, cfg), cfg, prev if isinstance(prev, dict) else None,
                     local_offset(now), axes, mult)
        _write_json(os.path.join(sd, 'decision.json'), dec)
    return dec, ctl, count_agents(sd, now, cfg)


def cmd_statusline(raw: dict, cfg: dict, now: float) -> str:
    dec, ctl, active = _statusline_state(raw, cfg, now)
    return render_segment(dec, ctl, cfg, now, active)


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


def _last_assistant_ts(path: str):
    """Epoch of the newest assistant entry in a jsonl transcript (tail scan), or None."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().split(bytes([10]))
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('type') == 'assistant' and row.get('timestamp'):
            try:
                return datetime.datetime.fromisoformat(str(row['timestamp']).replace('Z', '+00:00')).timestamp()
            except ValueError:
                continue
    return None


def _transcript_for(raw: dict):
    """Transcript of the agent that issued this call: its own file when `agent_id` is known, else the
    most recently written one of the session (the caller just wrote its tool_use message)."""
    path = raw.get('transcript_path')
    if not path:
        return None
    sid = raw.get('session_id') or os.path.splitext(os.path.basename(path))[0]
    subdir = os.path.join(os.path.dirname(path), sid, 'subagents')
    agent = raw.get('agent_id')
    if agent:
        own = os.path.join(subdir, f'agent-{agent}.jsonl')
        if os.path.exists(own):
            return own
    best, best_m = path, -1.0
    for cand in [path] + glob.glob(os.path.join(subdir, 'agent-*.jsonl')):
        try:
            m = os.path.getmtime(cand)
        except OSError:
            continue
        if m > best_m:
            best, best_m = cand, m
    return best


def _response_ts(raw: dict):
    """End of the model response that issued this tool call, or None when it cannot be read."""
    path = _transcript_for(raw)
    return _last_assistant_ts(path) if path else None


def cmd_enforce(raw: dict, cfg: dict, now: float, sleep=time.sleep):
    sd = state_dir()
    ctl = read_ctl(sd)
    tool = raw.get('tool_name', '')
    if not acting_axes(ctl) or tool in cfg['exempt_tools']:
        return None
    dec = _read_decision(sd, now)
    fresh = isinstance(dec, dict) and now - dec.get('ts', 0) <= cfg['stale_s']
    # a decision of a 5h window that already reset: level 3 and the 5h delay are gone, the weekly part stays
    expired = fresh and (dec.get('r5') or float('inf')) <= now
    if expired and dec.get('level') == 3:
        fresh = False
    if tool in ('Agent', 'Task'):
        stype = (raw.get('tool_input') or {}).get('subagent_type') or ''
        if stype not in cfg['exempt_agents'] and not claim_agent(sd, now, cfg, fresh and dec.get('level', 0) >= 2):
            return _deny(f'pacing: usage is ahead of its budget, {cfg["n_max"]} subagents already running. '
                         'Run this work sequentially; do not respawn until one finishes.')
    if not fresh:
        return None
    if expired:
        dec = {**dec, 'delay_5h': 0.0}
    delay = effective_delay(dec, ctl, cfg)
    if delay > 0.5:
        # Cache guard: pace adds at most `cap_s` per model turn and never pushes the gap since the last
        # response past `cache_ttl_s - cache_margin_s`, so the next request still hits the cheap cache.
        tpath = _transcript_for(raw)
        who = os.path.basename(tpath) if tpath else _safe_sid(raw.get('agent_id')) if raw.get('agent_id') else 'main'
        key = f'{_safe_sid(raw.get("session_id"))}.{who}'
        marker = os.path.join(sd, f'slept.{key}')
        state = _read_json(marker)
        state = state if isinstance(state, dict) else {}
        ceiling = cfg['cache_ttl_s'] - cfg['cache_margin_s']
        resp = _last_assistant_ts(tpath) if tpath else None
        if resp is not None:
            turn, same = resp, state.get('turn') == resp
        else:  # turn boundary unknown: one budget per full TTL window (past it the cache is gone anyway)
            same = now - state.get('turn', 0.0) < cfg['cache_ttl_s']
            turn = state['turn'] if same else now
        used = float(state.get('slept', 0.0)) if same else 0.0
        allowed = min(delay, cfg['cap_s'] - used, ceiling - max(0.0, now - turn))
        if allowed >= 0.5:
            sleep(allowed)
            try:
                _write_json(marker, {'turn': turn, 'slept': used + allowed})
            except OSError:
                pass
    return None


def _dq(p: str) -> str:
    """A path inside double quotes for the shell that runs the status line (POSIX expands `$`, backtick and `\\`)."""
    return p if os.name == 'nt' else re.sub(r'([$`"\\])', r'\\\1', p)


def _stable_target(cfg_dir: str, src: str) -> str:
    """Where the status line runs the script from: a plugin's own path changes on every update, so it gets a
    copy under the config dir; the owner's `hooks/pacing.py` is already stable and is used as is."""
    own = os.path.join(cfg_dir, 'hooks', 'pacing.py')
    same = os.path.normcase(src) == os.path.normcase(own)
    return src if same else os.path.join(cfg_dir, 'pace', 'pacing.py')


def refresh_stable_copy(cfg_dir: str, src: str) -> None:
    """After a plugin update the hooks run the new script while the status line runs the copy `setup` made:
    keep that copy in step. Does nothing unless `setup` already created it."""
    cfg_dir, src = os.path.abspath(cfg_dir), os.path.abspath(src)
    target = os.path.join(cfg_dir, 'pace', 'pacing.py')
    try:
        if os.path.normcase(src) != os.path.normcase(target) and os.path.exists(target) \
                and not filecmp.cmp(src, target, shallow=False):
            shutil.copyfile(src, target)
    except OSError:
        pass


GUIDE_NAME = 'pacing-cadencer-readme.html'


def find_guide(src: str):
    """The manual that ships with this copy of pace: next to the script, or in `<root>/docs` (hooks/ and
    scripts/ both sit one level under the root of the checkout and of the plugin package)."""
    here = os.path.dirname(os.path.abspath(src))
    for cand in (os.path.join(here, GUIDE_NAME), os.path.join(here, '..', 'docs', GUIDE_NAME)):
        if os.path.isfile(cand):
            return os.path.normpath(cand)
    return None


def _open_in_browser(path: str) -> bool:
    """Open `path` with the desktop's default handler without waiting for it or sharing the hook's streams.
    Console browsers and a missing display are left alone: a hook has 5 s and its stdout carries JSON."""
    try:
        import subprocess  # only `/pace guide` needs it; the hot hook paths do not pay for the import
        if sys.platform == 'win32':
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        if sys.platform == 'darwin':
            cmd = ['open', pathlib.Path(path).as_uri()]  # the URI goes to the default browser, not an editor
        elif os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            cmd = ['xdg-open', path]
        else:
            return False
        if not shutil.which(cmd[0]):
            return False
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        return False


def cmd_guide(src: str) -> str:
    """`/pace guide`: print where the manual is and try to open it; a headless session just gets the path."""
    path = find_guide(src)
    if not path:
        return f'pace guide: the manual {GUIDE_NAME} was not found next to {os.path.abspath(src)}.'
    opened = not os.environ.get('CLAUDE_PACING_NO_OPEN') and _open_in_browser(path)
    uri = pathlib.Path(path).as_uri()  # percent-encodes spaces and accents
    note = 'Asked the desktop to open it.' if opened else 'Open the link above in a browser.'
    return chr(10).join((f'pace guide: {path}', uri, note))


def cmd_setup(cfg_dir: str, src: str, silent: bool = False) -> str:
    """`/pace setup`: point `statusLine` at a stable copy of this script unless the user already has one.

    A plugin cannot ship a status line and no hook receives `rate_limits`, so without this entry pace has no
    samples. Nothing the user owns is overwritten: another status line is kept and a snippet is printed instead.
    `silent` installs `statusline --record-only`: pace samples and prints nothing, so the status line shows
    exactly what it showed before.
    """
    try:
        return _setup(os.path.abspath(cfg_dir), os.path.abspath(src), silent)
    except OSError as exc:
        return f'pace setup failed: {exc}'


def _setup(cfg_dir: str, src: str, silent: bool = False) -> str:
    posix = lambda p: p.replace(os.sep, '/')
    settings = os.path.realpath(os.path.join(cfg_dir, 'settings.json'))  # a symlinked dotfile stays a symlink
    out = [f'pace setup: python {sys.version.split()[0]} ok ({posix(sys.executable)})']
    conf, mode = {}, None
    if os.path.exists(settings):
        mode = os.stat(settings).st_mode & 0o777
        try:
            with open(settings, encoding='utf-8-sig') as f:
                conf = json.load(f)
        except (OSError, ValueError):
            return '\n'.join(out + [f'{posix(settings)} is not valid JSON: left untouched. Fix it, then run /pace setup again.'])
        if not isinstance(conf, dict):
            return '\n'.join(out + [f'{posix(settings)} is not a JSON object: left untouched.'])
    target = _stable_target(cfg_dir, src)
    py = f'"{_dq(posix(sys.executable))}"'  # an absolute path is the only name that always resolves
    for name in ('python', 'python3'):  # a bare name also parses in PowerShell, where a quoted word does not run
        found = shutil.which(name)
        if found and os.path.normcase(os.path.realpath(found)) == os.path.normcase(os.path.realpath(sys.executable)):
            py = name
            break
    if os.name == 'nt':
        # Git Bash and PowerShell both expand `$` and a backtick inside double quotes, and they escape them
        # differently: one quoting cannot be right for both, so the entry is not written. Only the paths that
        # enter the command count: a bare `python` name does not.
        quoted = [target] + ([sys.executable] if py.startswith('"') else [])
        bad = next((p for p in quoted if re.search(r'[$`"]', posix(p))), None)
        if bad:
            return '\n'.join(out + [f'cannot quote {posix(bad)} safely for the status line (it contains $, a '
                                    'backtick or a double quote): move it, then run /pace setup again.'])
    if os.path.normcase(target) != os.path.normcase(src):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(src, target)
    flags = '--record-only' if silent else '--full --standalone'  # not `mode`: that name holds the file mode
    command = f'{py} -S "{_dq(posix(target))}" statusline {flags}'
    cur = conf.get('statusLine')
    cur_cmd = str(cur.get('command', '')) if isinstance(cur, dict) else ''
    is_pace = 'pacing.py' in cur_cmd and 'statusline' in cur_cmd
    if is_pace and posix(target) in cur_cmd and ('--record-only' in cur_cmd) == silent:
        out.append('statusLine already points at pace: nothing changed.')
    elif cur and not is_pace:
        out.append(f'statusLine kept (it is yours): {cur_cmd or cur}')
        if silent:
            out.append(f'To feed pace without changing what it shows, add one line to your script, with the same '
                       f'stdin JSON: {py} -S "{posix(target)}" statusline --record-only >/dev/null')
        else:
            out.append(f'To add the pace segment, feed it the same stdin JSON: {py} -S "{posix(target)}" statusline '
                       '(prints `cadence: ...`).')
            out.append('To show one summary line after your own lines instead, run /pace template after and '
                       'follow the note it prints.')
    else:
        backup = None
        if os.path.exists(settings):
            backup = f'{settings}.pace-backup-{int(time.time())}'
            shutil.copy2(settings, backup)
        conf['statusLine'] = {'type': 'command', 'command': command}
        # the temp file carries the original mode before it replaces the file: it may hold secrets
        if not _write_text(settings, json.dumps(conf, indent=2, ensure_ascii=False) + '\n', mode):
            if backup:
                try:
                    os.remove(backup)
                except OSError:
                    pass
            return '\n'.join(out + [f'could not write {posix(settings)} (read-only or locked): nothing changed.'])
        what = 'repointed to this copy of pace' if is_pace else 'set'
        out.append(f'statusLine {what} in {posix(settings)}' + (' (previous file backed up).' if backup else '.'))
        out.append('A statusLine in settings.local.json, a project or managed settings overrides this one.')
    out.append('Needs Claude Code 2.1.80+ and a Pro or Max plan; API-key sessions send no usage data.')
    out.append('Next: /pace on (axes start OFF, observe only).')
    return '\n'.join(out)


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


def cmd_prompt(raw: dict, cfg: dict, now: float):
    # first: a refused state dir must not keep the status line on an old copy that still writes to a shared one
    refresh_stable_copy(config_dir(), __file__)
    sd = state_dir()
    cmd = parse_pace_prompt(raw.get('prompt') or '')
    ctl = read_ctl(sd)
    dec = _read_decision(sd, now) or {'state': 'na'}
    if cmd in ('setup', 'setup-silent'):
        first_use_menu(cmd, ctl)  # `setup` is one of the menu's answers: it marks, it shows nothing
        return {'decision': 'block', 'reason': cmd_setup(config_dir(), __file__, cmd == 'setup-silent')}
    if cmd == 'guide':
        return {'decision': 'block', 'reason': _reply(cmd, ctl, cmd_guide(__file__))}
    if cmd:
        if cmd not in ('status', 'brief'):
            ctl = apply_ctl(cmd, ctl, cfg)
            state_dir(create=True)
            _write_json(os.path.join(sd, 'ctl.json'), ctl)
        show = format_brief if cmd == 'brief' else format_status
        text = show(dec, ctl, cfg, now, count_agents(sd, now, cfg))
        return {'decision': 'block', 'reason': _reply(cmd, ctl, text)}
    state = dec.get('state', 'na')
    acting = acting_axes(ctl)
    if acting and state in ('ok', 'L1', 'L2', 'L3') and ctl.get('notified') != state \
            and now - dec.get('ts', 0) <= cfg['stale_s']:
        latest = read_ctl(sd)  # re-read: another session may have switched pacing off meanwhile
        if acting_axes(latest):
            latest['notified'] = state
            _write_json(os.path.join(sd, 'ctl.json'), latest)
            why = []
            if 'wk' in acting and dec.get('lead', 0) > cfg['deadband_pct']:
                why.append(f'weekly use is {dec["lead"]:+.0f}% over budget')
            if state == 'L3':
                why.append('the 5h window is nearly full')
            elif 'h5' in acting and dec.get('deficit5', 0) > cfg['deadband5_pct']:
                why.append('the 5h window is on pace to run out before it resets')
            if state != 'ok' and why:
                return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': (
                    f'pacing {state}: {" and ".join(why)}; tool calls are deliberately delayed. '
                    'Do not retry or work around the delay.')}}
    return None


def _ctl(argv: list[str], cfg: dict, now: float) -> int:
    cmd = parse_pace_prompt('pace ' + ' '.join(argv[2:])) or 'status'
    if cmd in ('setup', 'setup-silent'):
        first_use_menu(cmd, {})
        print(cmd_setup(config_dir(), __file__, cmd == 'setup-silent'))
        return 0
    if cmd == 'guide':
        print(_reply(cmd, read_ctl(state_dir()), cmd_guide(__file__)))
        return 0
    sd = state_dir(create=True)
    view_only = cmd in ('status', 'brief')
    ctl = read_ctl(sd) if view_only else apply_ctl(cmd, read_ctl(sd), cfg)
    if not view_only:
        _write_json(os.path.join(sd, 'ctl.json'), ctl)
    dec = _read_decision(sd, now) or {'state': 'na'}
    show = format_brief if cmd == 'brief' else format_status
    print(_reply(cmd, ctl, show(dec, ctl, cfg, now, count_agents(sd, now, cfg))))
    return 0


def main(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else ''
    full = '--full' in argv[2:]
    now = time.time()
    try:
        cfg = load_config()
    except Exception as exc:  # a bad value in the user's config must not become exit 1 and a re-run by the chain
        cfg = dict(DEFAULTS)
        try:
            _write_text(os.path.join(state_dir(create=True), 'pacing.err'), f'{time.time()} config {exc!r}')
        except OSError:
            pass
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if sub == 'ctl':
        try:
            return _ctl(argv, cfg, now)
        except Exception as exc:  # fail-open: a half-applied `ctl up` must not exit 1 and be re-run by the chain
            print(f'pace ctl failed: {exc!r}')
            return 0
    try:
        stdin = sys.stdin.read()
        raw = json.loads(stdin) if stdin.strip() else {}
        valid = isinstance(raw, dict)
        if not valid:
            raw = {}
        if sub == 'statusline' and full:
            if valid:  # no output makes the shell script fall back to its own layout
                print(cmd_statusline_full(raw, cfg, now, os.environ.get('CLAUDE_STATUS_GIT', ''),
                                          'L1' if '--standalone' in argv[2:] else '', '--after' in argv[2:]))
        elif sub == 'statusline' and '--record-only' in argv[2:]:
            _statusline_state(raw, cfg, now)  # sample + decision for `enforce`, no output: the status line is untouched
        elif sub == 'statusline':
            print(cmd_statusline(raw, cfg, now))
        elif sub == 'enforce':
            out = cmd_enforce(raw, cfg, now)
            if out:
                print(json.dumps(out))
        elif sub == 'prompt':
            out = cmd_prompt(raw, cfg, now)
            if out:
                print(json.dumps(out))
        elif sub == 'agent-start':
            agent_started(state_dir(), cfg, str(raw.get('agent_type') or ''))
        elif sub == 'agent-done':
            agent_stopped(state_dir(), cfg, str(raw.get('agent_type') or ''))
    except Exception as exc:  # fail-open: pacing must never break a session
        try:
            _write_text(os.path.join(state_dir(create=True), 'pacing.err'), f'{time.time()} {sub} {exc!r}')
        except OSError:
            pass
        if sub == 'statusline' and not full:
            print('cadence: [off]')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
