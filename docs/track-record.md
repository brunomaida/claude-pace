# Track record

How pace got from 0.9 to 0.14: what shipped, what review found, what was fixed, and what has not been run. Dates are commit dates. Every number below comes from the repository, its pull requests or the CI runs; where something was not run, it says so.

## Timeline

| Date | Version | Change | Where |
|---|---|---|---|
| 2026-10-14 | 0.9 | Initial release: observe-only pacing of tool calls and subagent slots, `/pace` command, status line | `ce6e221` |
| 2026-10-14 | 0.10 | `pace setup` crash on macOS and Linux fixed, tests made platform-neutral | `7008fa7`, `9232870`, tag `v0.10` |
| 2026-10-14 | 0.11 | `allowed-tools` rules matched to the quoted script path, CI matrix widened, matrix results and the PowerShell limit documented | `e57713f`, `3261031` |
| 2026-10-14 | 0.12 | Tune panel table shown, manual charset declared, host checks documented | `1b968e7`, tag `v0.12` |
| 2026-10-15 | 0.12 | README intro replaced with benefit bullets and a before/after figure | PR #3 |
| 2026-10-15 | 0.13 | `template after` (one summary line after your own status line) and a one-time first-use menu | issue #1, PR #2 |
| 2026-10-15 | 0.14 | Security hardening of the state directory, temp files and state read-back; `SECURITY.md` | issue #4, PR #5 |
| 2026-10-15 | 0.14 | Host check of the anchored `allowed-tools` rules recorded, test count corrected | PR #6, tag `v0.14` |

0.11 and 0.13 were not tagged; the 0.14 release notes cover 0.13.

## Reviews

Significant changes were reviewed read-only at the Opus tier, with the instruction to report only what could be pointed to in the code. The template feature had one review; the hardening had a security review and two reviews of the fixes.

| Change | Review | Result | Outcome |
|---|---|---|---|
| `template after` (0.13) | Whole-branch review | 2 Important, 3 minor | All fixed before merge (`d1a51a3`, `71ce31b`) |
| Whole plugin, security | Security review | 1 High, 2 Medium, 4 Low, 4 Info | Issue #4 |
| Hardening, first pass | Review of `ba49d0c` | "Fix first": the state directory was not validated for readers, `refresh_stable_copy` ran after it, numeric fields were unchecked | Fixed in `802f0c8` |
| Hardening, second pass | Review of `15faf26` | "Fix first": the `allowed-tools` rule still let `-c<code>#pacing.py` through; rate keys kept raw; `inf`/`nan` accepted | Fixed in `70d5594`, merged |

### Security findings and their state

| Severity | Finding | State |
|---|---|---|
| High | Shared `/tmp/claude-pacing` plus a predictable `.<pid>.tmp` name: a local user could redirect a write through a symlink (Linux) | Fixed: per-user directory, validated on every use, exclusive 0600 temp file |
| Medium | A decision file dated in the future could force or hold a pause | Fixed: future-dated decisions and slots are ignored |
| Medium | Wide `*` in `allowed-tools` | Narrowed twice; anchored to a path starting at `/` or at a drive letter. Any file whose path ends in `pacing.py` still matches (documented) |
| Low | Escape sequences reachable through decision fields and rate keys | Fixed: state whitelist, numeric and finite checks, keys re-rendered |
| Low | `settings.json` temp file mode, clock skew, readable data | Fixed or documented in `SECURITY.md` |
| Info | `_dq` quoting on Windows | `/pace setup` refuses a path it cannot quote |
| Info | Session ids used in file names | Reduced to a plain token |
| Info | Google fonts in the manual | Removed; the manual loads nothing from the network |
| Info | `statusLine` persists after uninstall | Documented |
| Declined | Whether the host applies the `allowed-tools` pattern before or after splitting a command at `;`, `&&`, `$(...)` | Not verifiable from this repository; stated in `SECURITY.md` |

## Verification

| Check | Result |
|---|---|
| Package suite (`tests/test_pacing.py`, `tests/test_install.py`) | 273 tests, 14 skipped (documented host limits and POSIX-only cases on a Windows run) |
| CI matrix | 3 operating systems × Python 3.9 to 3.14, 42 checks, green on every merge |
| POSIX-only hardening tests | Run on Linux and macOS in CI; one stale test failed there after the first hardening pass and was fixed in `15faf26` |
| Host, Claude Code 2.1.300 on Windows 11 | Plugin hooks load; `/pace:pace status` runs the command body with no permission denial under the anchored rules; a decided pause is slept for a Bash and a Read call; a real subagent takes and releases a slot |
| Regression tests | A `Hardening` test class covers the findings; the tests for rate keys and non-finite numbers were run against the previous code and fail there |

## Not covered

- The host on macOS or Linux (only CI runs the suite there).
- `/pace setup` through the host: it rewrites a real `settings.json`.
- Linux distributions other than Ubuntu, Debian, Fedora and Alpine.
- Windows PowerShell 5.1 without Git for Windows: the `||` fallback in the hook commands does not run there.
- The browser window opened by `/pace guide` was not observed.

## Process

- Each change went through a written spec and plan where it altered behaviour (`docs/superpowers/`), then issue, branch, pull request, CI, merge.
- Security-relevant branches were reviewed at a tier at or above the one that wrote the code.
- Private vulnerability reporting is on for the repository; see [SECURITY.md](../SECURITY.md).
