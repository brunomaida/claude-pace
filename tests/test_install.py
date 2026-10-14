"""Install simulator: run the plugin's hooks and command the way the host would, in a hermetic profile.

Everything runs against a copy of this package placed in a path with a space and an accent, with HOME, TEMP and
the Claude config dir pointing into a temporary directory. The host itself is not involved: what is checked is
that the shipped `hooks/hooks.json`, `commands/pace.md` and `scripts/pacing.py` work from an install path on this
machine, through the shells and Python launchers a user can have. A launcher or shell that is not available here
is skipped with its reason, never reported as a pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 90
STATUS_PAYLOAD = {
    'session_id': 's1',
    'model': {'display_name': 'Claude'},
}


def _git_bash():
    if os.name != 'nt':
        return None
    git = shutil.which('git')
    cands = []
    if git:
        gdir = Path(git).resolve().parent
        cands += [gdir.parent / 'bin' / 'bash.exe', gdir.parent.parent / 'bin' / 'bash.exe']
    cands += [Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'bin' / 'bash.exe']
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def shells():
    """[(name, argv prefix)] for the shells a host can run hook commands with on this machine."""
    out = []
    if os.name == 'nt':
        bash = _git_bash()
        if bash:
            out.append(('git-bash', [bash, '-c']))
        for name in ('powershell', 'pwsh'):  # Windows PowerShell 5.1 and, when installed, PowerShell 7
            exe = shutil.which(name)
            if exe:
                out.append((name, [exe, '-NoProfile', '-NonInteractive', '-Command']))
    elif os.path.exists('/bin/sh'):
        out.append(('sh', ['/bin/sh', '-c']))
    return out


def split_command(template: str):
    """({interpreter: command}, tail) of a shipped `a || b || c [|| exit 1]` hook or command line."""
    parts = template.split(' || ')
    tail = [parts.pop()] if parts[-1] == 'exit 1' else []
    by = {part.split(' -S ')[0]: part for part in parts}
    assert sorted(by) == ['py -3', 'python', 'python3'], template
    return by, tail


def command_for(template: str, launcher: str, shell: str) -> str:
    """Pick, from the shipped command, what a given launcher situation runs.

    chain: as shipped. py -3 / python3 / python: that alternative alone. fallback / fallback2: one / two
    interpreters that do not exist are put in front of the shipped chain, so `||` must fall through them to the
    first interpreter this machine has. absolute: the `python` alternative with this process's absolute
    interpreter path (what `pace setup` writes).
    """
    by, tail = split_command(template)
    if launcher == 'chain':
        return template
    if launcher in by:
        return by[launcher]
    names = list(by)
    if launcher in ('fallback', 'fallback2'):
        missing = 1 if launcher == 'fallback' else 2
        alts = [f'pace-no-such-python{i} -S pacing.py' for i in range(missing)] + [by[n] for n in by]
        return ' || '.join(alts + tail)
    if launcher == 'absolute':
        exe = sys.executable.replace(chr(92), '/') if shell == 'git-bash' else sys.executable
        head = f'& "{exe}"' if shell.startswith(('powershell', 'pwsh')) else f'"{exe}"'
        return head + by['python'][len('python'):]
    raise AssertionError(launcher)


def launcher_available(name: str) -> bool:
    if name in ('chain', 'fallback', 'fallback2', 'absolute'):
        return True
    return shutil.which(name.split()[0]) is not None


def chain_supported(shell_name: str, argv) -> str:
    """Empty string when `a || b` falls through a missing command `a`; else the reason.

    Windows PowerShell 5.1 has no `||` at all; PowerShell 7 parses it but treats a missing command as an error,
    not as a failed command, so the right side never runs.
    """
    probe = 'pace-no-such-command || exit 0'
    r = subprocess.run(list(argv) + [probe], capture_output=True, timeout=TIMEOUT)
    if r.returncode == 0:
        return ''
    lines = r.stderr.decode('utf-8', 'replace').strip().splitlines()
    return (lines[0] if lines else f'exit {r.returncode}')[:160]


def _hook_commands() -> dict:
    data = json.loads((ROOT / 'hooks' / 'hooks.json').read_text(encoding='utf-8'))
    return {event: groups[0]['hooks'][0] for event, groups in data['hooks'].items()}


def _command_line_from_markdown() -> str:
    text = (ROOT / 'commands' / 'pace.md').read_text(encoding='utf-8')
    m = re.search(r'`(python3 -S[^`]*pacing[.]py[^`]*)`', text)
    assert m, 'commands/pace.md has no command line'
    return m.group(1)


def _package_version() -> str:
    code = (ROOT / 'scripts' / 'pacing.py').read_text(encoding='utf-8')
    return re.search(r"^VERSION = '([^']+)'", code, re.M).group(1)


class Profile:
    """A throwaway user profile plus a copy of the package, both under a temporary directory."""

    def __init__(self, base: Path, plugin_root: Path):
        self.base = base
        self.plugin_root = plugin_root
        self.home = base / 'Usér Home'
        self.tmp = base / 'tmp dir'
        self.cfg = self.home / '.claude'
        self.state = self.tmp / 'claude-pacing'
        for d in (self.home, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('CLAUDE_', 'ANTHROPIC_'))}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), TEMP=str(self.tmp), TMP=str(self.tmp),
                   TMPDIR=str(self.tmp), CLAUDE_CONFIG_DIR=str(self.cfg), CLAUDE_PLUGIN_ROOT=str(plugin_root),
                   CLAUDE_PACING_NO_OPEN='1')
        self.env = env

    def expand(self, command: str, style: str = 'native') -> str:
        root = self.plugin_root.as_posix() if style == 'posix' else str(self.plugin_root)
        return command.replace('${CLAUDE_PLUGIN_ROOT}', root)

    def run(self, shell_argv, command: str, stdin: str = '', env=None, timeout: int = TIMEOUT):
        t = time.monotonic()
        r = subprocess.run(list(shell_argv) + [command], input=stdin.encode('utf-8'), capture_output=True,
                           env=env or self.env, cwd=str(self.home), timeout=timeout)
        return r.returncode, r.stdout.decode('utf-8', 'replace'), r.stderr.decode('utf-8', 'replace'), time.monotonic() - t

    def ctl(self, *args: str, env=None):
        r = subprocess.run([sys.executable, '-S', str(self.plugin_root / 'scripts' / 'pacing.py'), 'ctl', *args],
                           capture_output=True, env=env or self.env, cwd=str(self.home), timeout=TIMEOUT)
        return r.returncode, r.stdout.decode('utf-8', 'replace')


def _status_payload() -> str:
    now = int(time.time())
    return json.dumps(dict(STATUS_PAYLOAD, rate_limits={
        'five_hour': {'used_percentage': 37, 'resets_at': now + 3600},
        'seven_day': {'used_percentage': 71, 'resets_at': now + 4 * 86400}}))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.base = Path(cls._dir.name)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def profile(self, name: str) -> Profile:
        base = self.base / name
        root = base / 'Pace Plügin' / 'claude-pace'
        shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        return Profile(base, root)


class HookFlow(Base):
    """The four hook events, the command line and the status line, through each shell and launcher."""

    def _flow(self, launcher: str):
        if not shells():
            self.skipTest('no shell available on this machine')
        for shell_name, argv in shells():
            for style in (('native', 'posix') if os.name == 'nt' else ('native',)):
                label = f'{shell_name}/{launcher}/{style}'
                with self.subTest(label):
                    if launcher in ('chain', 'fallback', 'fallback2'):
                        why = chain_supported(shell_name, argv)
                        if why:
                            self.skipTest(f'{shell_name} does not fall through `a || b` when `a` is missing '
                                          f'({why[:90]}); Claude Code documents PowerShell for hooks only when '
                                          'Git Bash is not installed')
                    self._one(label.replace('/', '_').replace(' ', ''), shell_name, argv, launcher, style)

    def _command(self, p: Profile, template: str, shell_name: str, launcher: str, style: str, args: str = '') -> str:
        return p.expand(command_for(template, launcher, shell_name).replace('$ARGUMENTS', args), style)

    def _one(self, name: str, shell_name: str, argv, launcher: str, style: str):
        p = self.profile(name)
        hooks = _hook_commands()
        cmd = lambda event: self._command(p, hooks[event]['command'], shell_name, launcher, style)
        run = lambda command, stdin='': p.run(argv, command, stdin)

        # UserPromptSubmit: `pace on` is intercepted and persisted
        rc, out, err, _ = run(cmd('UserPromptSubmit'), json.dumps({'prompt': 'pace on', 'session_id': 's1'}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['decision'], 'block', out)
        ctl = json.loads((p.state / 'ctl.json').read_text(encoding='utf-8'))
        self.assertTrue(ctl['wk']['on'])

        # the command from commands/pace.md: status, guide and setup
        md = _command_line_from_markdown()
        rc, out, err, _ = run(self._command(p, md, shell_name, launcher, style, 'status'))
        self.assertEqual(rc, 0, err)
        self.assertIn(f'pace v{_package_version()}', out)

        rc, out, err, _ = run(self._command(p, md, shell_name, launcher, style, 'guide'))
        self.assertEqual(rc, 0, err)
        guide = Path(out.splitlines()[0].split('pace guide: ', 1)[1].strip())
        self.assertTrue(guide.is_file(), out)
        self.assertEqual(guide.name, 'pacing-cadencer-readme.html')

        rc, out, err, _ = run(self._command(p, md, shell_name, launcher, style, 'setup'))
        self.assertEqual(rc, 0, err)
        settings = json.loads((p.cfg / 'settings.json').read_text(encoding='utf-8'))
        self.assertTrue((p.cfg / 'pace' / 'pacing.py').is_file(), out)

        # the status line pace wrote runs from the stable copy and prints a line
        status_cmd = settings['statusLine']['command']
        if shell_name in ('powershell', 'pwsh') and status_cmd.lstrip().startswith('"'):
            # setup wrote a quoted absolute interpreter (the launcher is not the `python` on PATH); PowerShell
            # cannot run a quoted first token, so this combination is a known gap rather than a pass
            self.skipTest(f'{shell_name} cannot run the quoted status line command: {status_cmd[:60]}')
        rc, out, err, _ = run(status_cmd, _status_payload())
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip(), 'status line printed nothing')
        self.assertTrue((p.state / 'decision.json').is_file())

        # PreToolUse: a decided delay is slept for real
        decision = {'ts': time.time(), 'state': 'L1', 'level': 1, 'delay_rec': 1.2}
        (p.state / 'decision.json').write_text(json.dumps(decision), encoding='utf-8')
        rc, out, err, took = run(cmd('PreToolUse'), json.dumps({'tool_name': 'Bash', 'session_id': 's1'}))
        self.assertEqual((rc, out.strip()), (0, ''), err)
        self.assertGreaterEqual(took, 1.0, 'enforce did not pause')

        # SubagentStart / SubagentStop: a claimed slot is confirmed, then released
        decision = {'ts': time.time(), 'state': 'L2', 'level': 2, 'delay_rec': 0}
        (p.state / 'decision.json').write_text(json.dumps(decision), encoding='utf-8')
        spawn = {'tool_name': 'Agent', 'session_id': 's1', 'tool_input': {'subagent_type': 'general-purpose'}}
        rc, out, err, _ = run(cmd('PreToolUse'), json.dumps(spawn))
        self.assertEqual((rc, out.strip()), (0, ''), err)
        agents = p.state / 'agents'
        self.assertEqual(len(list(agents.glob('*.p'))), 1)
        event = {'agent_type': 'general-purpose', 'session_id': 's1'}
        rc, _, err, _ = run(cmd('SubagentStart'), json.dumps(event))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(list(agents.glob('*.a'))), 1)
        rc, _, err, _ = run(cmd('SubagentStop'), json.dumps(event))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(list(agents.glob('*.a'))), 0)

        self.assertFalse((p.state / 'pacing.err').exists(), 'a hook failed open and left pacing.err')

    def test_launcher_chainAsShipped(self):
        self._flow('chain')

    def test_launcher_firstInterpreterMissing_fallsThrough(self):
        self._flow('fallback')

    def test_launcher_firstTwoInterpretersMissing_reachTheLast(self):
        self._flow('fallback2')

    def test_launcher_python(self):
        if not launcher_available('python'):
            self.skipTest('`python` is not on PATH')
        self._flow('python')

    def test_launcher_python3(self):
        if not launcher_available('python3'):
            self.skipTest('`python3` is not on PATH')
        self._flow('python3')

    def test_launcher_pyWindowsLauncher(self):
        if not launcher_available('py -3'):
            self.skipTest('the `py` launcher is not on PATH')
        self._flow('py -3')

    def test_launcher_absolutePath(self):
        self._flow('absolute')

    def _hook_shell(self):
        for name, argv in shells():
            if not chain_supported(name, argv):
                return name, argv
        self.skipTest('no shell on this machine parses `a || b` (Git Bash or sh needed)')

    def test_missingPython_failsVisiblyAndDoesNotHang(self):
        name, argv = self._hook_shell()
        p = self.profile('nopython')
        empty = p.base / 'empty path'
        empty.mkdir()
        env = dict(p.env, PATH=str(empty))
        command = p.expand(_hook_commands()['UserPromptSubmit']['command'])
        rc, out, _, took = p.run(argv, command, json.dumps({'prompt': 'pace on'}), env=env, timeout=30)
        self.assertEqual(rc, 1, f'{name}: a missing python must be a plain hook failure (1), never 0 or blocking 2')
        self.assertEqual(out.strip(), '')
        self.assertLess(took, 30)

    def test_missingScript_isNeverABlockingExitCode(self):
        """python exits 2 when it cannot open the script; for a hook, 2 would block every tool call."""
        name, argv = self._hook_shell()
        p = self.profile('noscript')
        command = _hook_commands()['PreToolUse']['command'].replace('${CLAUDE_PLUGIN_ROOT}', str(p.base / 'gone'))
        rc, out, _, _ = p.run(argv, command, json.dumps({'tool_name': 'Bash'}), timeout=30)
        self.assertEqual(rc, 1, f'{name}: exit code {rc}')
        self.assertEqual(out.strip(), '')


class SetupVariants(Base):
    """`pace setup` against the settings.json shapes real users have."""

    def _setup(self, p: Profile, *args: str):
        return p.ctl('setup', *args)

    def test_noSettingsFile_createsStatusLineAndStableCopy(self):
        p = self.profile('nosettings')
        rc, out = self._setup(p)
        self.assertEqual(rc, 0, out)
        settings = json.loads((p.cfg / 'settings.json').read_text(encoding='utf-8'))
        self.assertIn('statusline --full --standalone', settings['statusLine']['command'])
        self.assertEqual((p.cfg / 'pace' / 'pacing.py').read_bytes(), (p.plugin_root / 'scripts' / 'pacing.py').read_bytes())

    def test_setupSilent_installsTheRecordOnlyStatusLine(self):
        p = self.profile('silent')
        rc, out = self._setup(p, 'silent')
        self.assertEqual(rc, 0, out)
        settings = json.loads((p.cfg / 'settings.json').read_text(encoding='utf-8'))
        self.assertIn('--record-only', settings['statusLine']['command'])

    def test_invalidJson_isLeftUntouched(self):
        p = self.profile('badjson')
        p.cfg.mkdir(parents=True)
        (p.cfg / 'settings.json').write_text('{not json', encoding='utf-8')
        rc, out = self._setup(p)
        self.assertEqual(rc, 0, out)
        self.assertEqual((p.cfg / 'settings.json').read_text(encoding='utf-8'), '{not json')
        self.assertIn('not valid JSON', out)

    def test_usersOwnStatusLine_isKept(self):
        p = self.profile('ownstatus')
        p.cfg.mkdir(parents=True)
        original = json.dumps({'statusLine': {'type': 'command', 'command': 'echo mine'}, 'other': 1})
        (p.cfg / 'settings.json').write_text(original, encoding='utf-8')
        rc, out = self._setup(p)
        self.assertEqual(rc, 0, out)
        self.assertEqual((p.cfg / 'settings.json').read_text(encoding='utf-8'), original)
        self.assertIn('statusLine kept', out)

    def test_afterAnUpdate_setupRefreshesTheStableCopy(self):
        p = self.profile('update')
        self.assertEqual(self._setup(p)[0], 0)
        script = p.plugin_root / 'scripts' / 'pacing.py'
        script.write_bytes(script.read_bytes() + b'\n# next release\n')
        rc, out = self._setup(p)
        self.assertEqual(rc, 0, out)
        self.assertEqual((p.cfg / 'pace' / 'pacing.py').read_bytes(), script.read_bytes())

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'directory permissions are not enforced on Windows or for root')
    def test_readOnlyConfigDir_failsOpenWithAMessage(self):
        p = self.profile('readonly')
        p.cfg.mkdir(parents=True)
        p.cfg.chmod(0o555)
        self.addCleanup(p.cfg.chmod, 0o755)
        rc, out = self._setup(p)
        self.assertEqual(rc, 0, out)
        self.assertIn('pace setup failed', out)


class Manual(Base):
    def test_theManualShipsWithThePackage(self):
        manual = ROOT / 'docs' / 'pacing-cadencer-readme.html'
        self.assertTrue(manual.is_file())
        self.assertIn(f'v{_package_version()}', manual.read_text(encoding='utf-8'))

    def test_guideFromTheInstalledCopy_pointsAtTheShippedManual(self):
        p = self.profile('guide')
        rc, out = p.ctl('guide')
        self.assertEqual(rc, 0, out)
        path = Path(out.splitlines()[0].split('pace guide: ', 1)[1].strip())
        self.assertEqual(path.resolve(), (p.plugin_root / 'docs' / 'pacing-cadencer-readme.html').resolve())


if __name__ == '__main__':
    unittest.main()
