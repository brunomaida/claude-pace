---
description: Pacing cadencer control (on|off|measure per axis wk|5h, up|down|auto|reset|status|brief|guide|setup [silent], template L1|L2|C1|C2|off)
argument-hint: [wk|5h] on | off | measure [on|off] | up | down | auto | reset | status | brief | guide | setup [silent] | template [L1|L2|C1|C2|off]
disable-model-invocation: true
allowed-tools: Bash(python3 -S *pacing.py ctl*), Bash(python -S *pacing.py ctl*), Bash(py -3 -S *pacing.py ctl*)
---

Run exactly this with the Bash tool and print its output verbatim, nothing else:

`python3 -S "${CLAUDE_PLUGIN_ROOT}/scripts/pacing.py" ctl $ARGUMENTS || python -S "${CLAUDE_PLUGIN_ROOT}/scripts/pacing.py" ctl $ARGUMENTS || py -3 -S "${CLAUDE_PLUGIN_ROOT}/scripts/pacing.py" ctl $ARGUMENTS`
