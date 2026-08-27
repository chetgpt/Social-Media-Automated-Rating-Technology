# Profile 7 terminal-blocker handling for TikTok MUSIC AUDIT

Use this reference only after the guarded MUSIC AUDIT task has ended with a
structured browser-related terminal result. Keep the exact handoff, run ID,
project directory, workflow database, master database, source, selection, and
attempt ledger. Never create or propose a replacement audit.

Do not enter this procedure while a fresh `MUSIC_AUDIT_HEARTBEAT` is appearing,
the managed task is `RUNNING`, the operator lock is held, or an
operator/collector process is live. Durable `collecting`, a blank log, or
`Last progress: never` is not crash evidence. Use the guarded operator's
read-only `poll` only when liveness or continuation eligibility is uncertain.

## Follow the guarded state

~~~powershell
$AuditPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$Operator = '.\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py'

& $AuditPython $Operator poll --handoff '<absolute-handoff-path>'
~~~

Interpret the result mechanically:

- `RUNNING`: wait on the original task. Do not recover or resume.
- `INTERRUPTED`: execute the returned `safe_same_handoff_action.argv` once.
- `RESUME_READY`: the terminal browser attempt is paired, no owner is live,
  and the current epoch has one guarded continuation left. When the non-AI
  user explicitly requests resume/continue, execute the returned argv exactly.
- `COLLECTION_COMPLETE_NEEDS_FINALIZE`: run offline `finalize`; do not touch
  Profile 7.
- `COMPLETE`: stop or run offline `validate`.
- `RESTART_PENDING`: follow only the legacy restart branch below.
- `BLOCKED`: preserve the run and report `next_action`; no continuation is
  authorized.

For `INTERRUPTED`, or for `RESUME_READY` after explicit user direction, the
guarded resume is the first browser-touching operation:

~~~powershell
& $AuditPython $Operator resume --handoff '<absolute-handoff-path>'
~~~

Do not precede it with `social_browser.py start`, `social_browser.py status`,
an Edge launch, a new operator `start`, raw `engage_tiktok.py resume-collect`,
or `--after-restart`. The guarded resume owns automatic Profile 7 startup or
reuse, active-account resolution, immutable run validation, and the attempt
claim. Keep its managed foreground task alive through all heartbeats and wait
for terminal JSON.

Only `resume_budget` from the guarded handoff establishes whether the epoch is
unused or exhausted. A failed low-level browser command does not consume that
budget and cannot prove exhaustion. A Gemini/Antigravity task restart, tool
server restart, app restart, or model claim is not a Windows restart.

## Human browser action

Do not ask the user merely to start Edge. The guarded resume starts or reuses
Profile 7 itself. Ask for browser interaction only when the paired terminal
result specifically establishes that the verified Profile 7 TikTok session is
logged out, its active handle still cannot be resolved after retry, or the
observed handle differs from an expected account. The user must log in, load
TikTok home/profile navigation, or switch accounts inside that same verified
Profile 7 window. After the user confirms the required action, use the guarded
same-handoff resume above; do not run a separate preflight first.

## Diagnostic-only commands

Standalone browser commands are not a continuation path. Use them only when
the user explicitly asks for browser diagnosis rather than asking to resume,
and only after the guarded task is terminal with no live owner:

~~~powershell
& $AuditPython .\social_browser.py start --json --startup-timeout 120
& $AuditPython .\social_browser.py status --json
~~~

Their result may describe reachability, Profile 7 identity, and TikTok login
state, but it must not alter the handoff, spend a guarded attempt, authorize
`--after-restart`, prove a native Edge crash, or justify a fresh project. Do not
loop these commands, run them in parallel with the operator, kill Edge, delete
browser-control/profile state, add headless/direct-port/replacement-user-data
flags, or patch the launcher/gates.

## Native Edge crash and legacy restart handoff

Command silence, generic bridge failure, browser unreachability, a stale
Windows event, or a model-supplied reason is not native-crash proof. The current
operator has no trusted PID/time-bound Windows fault-receipt channel and rejects
new model-issued `prepare-restart` requests with `human_action_required`.
Preserve and report that outcome. Do not fabricate evidence or tell the user to
restart.

The following route exists only when the handoff was already placed in
`RESTART_PENDING` by a legacy or externally trusted process and the user has
actually restarted Windows. Its first browser-touching operation is:

~~~powershell
& $AuditPython $Operator resume `
  --handoff '<absolute-handoff-path>' `
  --after-restart
~~~

The operator must independently observe a different Windows boot identifier
before it opens epoch 1. A flag, model/tool/server restart, or user wording is
not proof. If that one epoch fails, preserve and report; never loop or create a
replacement project.

If a legacy restart claim was prepared prematurely and Windows did not restart,
the guarded `cancel-restart` command may be used only when every predicate in
the guarded operator reference is already established. Resume afterward
without `--after-restart`. Never use cancellation for a paired terminal browser
failure.
