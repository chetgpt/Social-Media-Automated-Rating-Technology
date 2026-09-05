# Guarded MUSIC AUDIT operator

Use this procedure for Gemini/Antigravity execution, repeatable smoke tests, and
restart-safe continuation. The operator accepts only canonical MUSIC AUDIT
commands, invokes the required Python interpreter without a shell, captures the
real durable `engage_*` run ID, and generates the evidence review from local and
master state. It never calls `social_browser.py`, starts Edge directly, or
accepts arbitrary browser flags.

Each operator child accepts exactly one topic, creator, or URL source. For a
new topic child it freezes `topic_query_policy=exact`: the normalized
user-supplied topic is the sole TikTok query, and pagination/retries repeat that
same query. Neither `--posts`, the candidate reserve, nor a page/retry budget
may generate related terms or modifiers. A saved legacy
`related_variants_v1` handoff resumes unchanged as a compatibility path; never
use that policy for a new child.

Set the workspace and interpreter once:

~~~powershell
Set-Location -LiteralPath 'D:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'
$AuditPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$Operator = '.\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py'
~~~

## Start a one-post topic-discovered smoke test

~~~powershell
& $AuditPython $Operator start --run-label 'test' --topic 'music' --posts 1
~~~

This searches only the normalized query `music`. A larger `--posts N` changes
the exact-count target and same-query pagination work; it does not create
count-scaled keyword combinations. Exhaustion or platform refusal produces an
honest `collection_incomplete: X/N` review.

When `--project` is omitted, the operator creates a unique semantic project
name from the optional label, collection policy, actual source type and target,
cardinality, timestamp, and microseconds. The command above therefore starts
with a name such as
`music_audit_test_new_topic_music_1p_<timestamp>_<microseconds>`. `--run-label`
is naming metadata only; use it for a purpose the operator cannot infer, such
as `test` or `brand-mie-sedaap`. It never changes the source or collection
scope. Omit it for the normal derived topic, creator, or URL name.

A supplied advanced `--project` must be lowercase, begin with `music_audit_`,
fit the operator's Windows-safe length bound, and omit the `project_` folder
prefix. `--project` and `--run-label` are mutually exclusive. Long automatic
names are shortened deterministically with a hash. The operator runs the
canonical `engage_tiktok.py music-audit` command with `new_only`, 20 comments,
and a 120-second browser startup timeout. It stores a hash-bound handoff inside
the generated project folder.

~~~text
comments_data/project_<project>/<artifact-stem>_handoff.json
~~~

Every guarded run owns exactly one canonical project folder:

~~~text
comments_data/project_<project>/
|-- state/
|   `-- <artifact-stem>_state.sqlite
|-- <artifact-stem>_handoff.json
|-- <artifact-stem>_ledger.jsonl
|-- <artifact-stem>_evidence.jsonl      # only after validated completion
|-- <artifact-stem>_review.json         # machine-readable result
`-- <artifact-stem>_review.md           # rendered result
~~~

Do not create `results.json`, `audit_results.json`, a project CSV, or a separate
`comments_data/exports` file for a guarded MUSIC AUDIT. The evidence rows belong
in `<artifact-stem>_evidence.jsonl`; the result summary is
`<artifact-stem>_review.json`. The compact artifact stem is derived and
hash-bound before collection so copied files still identify their run without
creating unsafe Windows path lengths. Blocked or interrupted runs correctly
omit the evidence export while preserving their database, handoff, ledger, and
review. Existing layout-v1 handoffs retain their fixed historic names and must
never be renamed; the operator automatically validates them through its legacy
layout path. The shared master registry remains outside the project folder at
`comments_data/tiktok_master/state/tiktok_master.sqlite`.

This is one managed foreground task and may take several minutes. The
120-second value bounds one internal browser startup attempt; it is not a
wall-clock deadline for the operator or the full audit. While the collector is
alive, the operator emits a sanitized `MUSIC_AUDIT_HEARTBEAT` approximately
every 15 seconds. A fresh heartbeat with `status=RUNNING` and
`collector_running=true`, task-manager state `RUNNING`, a held operator lock,
or a live recorded operator/collector means: keep the exact task alive and wait
for its terminal JSON. Durable status `collecting` alone is nonterminal but does
not prove that the process survived; run the read-only `poll`. A blank task log,
`Last progress: never`, or no final JSON does not mean Edge crashed while a
liveness signal remains.

For Gemini/Antigravity, these rules are mandatory while that task is active:

- retain the exact managed task ID and wait/query that task;
- never use task cancellation, `manage_task kill`, `Stop-Process`, or another
  process command against the operator or collector Python process;
- never issue a second `start`, change buffering, or create a replacement
  project;
- never run `social_browser.py`, browser recovery, or `prepare-restart` in
  parallel; and
- never classify command silence, a heartbeat, or `collecting` as a failure.

The operator binds the real `engage_*` run ID and frozen selection as soon as
the durable database row appears, before collection finishes. An automatic
`start` also refuses an unfinished matching audit so that an interrupted run is
resumed instead of hidden by a duplicate.

From a separate short-lived task, safe progress can be read without touching
the browser or mutating the handoff, ledger, or workflow database:

~~~powershell
& $AuditPython $Operator poll --handoff '<absolute-handoff-path>'
~~~

Follow this table exactly; never infer failure from silence or from durable
`collecting` alone:

| `operator_status` | Meaning | Permitted next action |
|---|---|---|
| `RUNNING` | A worker, lock, or conservatively uncertain process identity still owns the run. | Keep the original foreground task alive and wait. A later read-only `poll` is allowed. |
| `INTERRUPTED` | No worker/lock is live and the handoff reports an unused safe continuation. | Run `resume --handoff '<absolute-handoff-path>'` once, with no low-level browser preparation and no `--after-restart`. This also retries a pure prelaunch failure without creating a project. |
| `RESUME_READY` | A paired terminal browser blocker exists, no worker/lock is live, and `resume_budget.attempts_remaining=1`. | If the non-AI user explicitly requested resume/continue, execute `safe_same_handoff_action.argv` exactly as the first browser-touching operation. Otherwise report that the same-run continuation remains available. |
| `COLLECTION_COMPLETE_NEEDS_FINALIZE` | The database reached the exact-count gate but export/review finalization did not finish. | Run `finalize --handoff '<absolute-handoff-path>'`; it is offline and does not touch Profile 7. |
| `COMPLETE` | Export and review validation completed. | Stop, or run the offline `validate --handoff '<absolute-handoff-path>'`. |
| `RESTART_PENDING` | A legacy/manual restart handoff exists. | Do not start or resume casually. Use the guarded false-claim cancellation only when all of its exact predicates below hold; otherwise stop for human review. |
| `BLOCKED` | The paired terminal result or guarded retry ledger does not authorize another same-run attempt. | Preserve the run and report `next_action`; never create a replacement or infer a restart. |

Always obey the returned `next_action`. In particular, an exhausted epoch is
`BLOCKED`, not permission to run another resume. A failed
`social_browser.py` command is diagnostic only: it does not consume the
reported budget and cannot prove exhaustion. A Gemini task/server/app restart
is not a Windows restart.

For an explicit multi-topic MUSIC AUDIT, do not place several topics in one
operator command or concatenate them into one query. First resolve the user's
TOTAL, EACH, or CUSTOM quota semantics, then use `music_audit_topics.py plan`.
That coordinator invokes these single-topic guarded children sequentially,
preserves their exact handoffs, and exposes
`collect|continue|status|validate --run-dir`. See the skill's multi-topic
section and `docs/contracts/MULTI_TOPIC_MUSIC_AUDIT.md`.

Use the same `start` command shape for another scope:

- topic: `--topic '<topic>' --posts N`;
- creator: `--creator '<@handle-or-profile-url>' --posts N`;
- creator ALL: `--creator '<target>' --all-posts`;
- direct URL: `--url '<canonical-url>' --posts 1`.

For an explicitly named brand run, prepend only a descriptive label, for
example `--run-label 'brand-mie-sedaap' --creator '@miesedaap' --posts 5`.
`brand` is not a fourth source mode: the actual source remains creator, topic,
or URL. Never infer `test` from the one-post shape; pass `--run-label 'test'`
only when the user actually requested a test.

Creator ALL has one exhaustive guarded shape:
`start --creator '<target>' --all-posts`. Never append `--posts`, `--max-pages`,
or another finite discovery bound; use `--posts N` when the requested scope is
bounded. The operator rejects the capped-ALL combination before creating a
project.

Add `--collection-policy refresh_known` only for an explicit MUSIC AUDIT
REFRESH request. The operator rejects incompatible source, cardinality, and
refresh options instead of falling back. It freezes the canonical direct-URL
post ID automatically, deduplicates explicit numeric refresh IDs, and resolves
the default 24-hour topic/creator cutoff to an absolute timestamp before run
creation so those values can be verified on resume.
After canonical run creation, the handoff also binds the ordered durable
refresh IDs, ordered candidate IDs, candidate count, full candidate-array hash,
and cutoff. An emptied, reordered, or altered candidate selection fails closed
on status, resume, finalize, and validation.

If collection completes, the operator automatically runs offline status,
exports evidence, validates local/master/hash bindings, and writes:

- `<artifact-stem>_evidence.jsonl`;
- `<artifact-stem>_ledger.jsonl`;
- `<artifact-stem>_review.json`; and
- `<artifact-stem>_review.md`.

Success is the validator result, not process exit alone: require
`status=collection_complete`, `evidence_ready=requested`, the exact export row
count, valid local/master bindings, terminal evidence/music outcomes, and empty
AI/outbound action lists. `unique_collected` and `failed` retain candidate
history and may exceed zero on a completed topic or creator run.
For creator ALL, the machine review must also show a verified complete terminal
frontier, `has_more=false`, `frontier_stop_reason=source_exhausted`, unique
exact-owner inventory count, selected-new count, `new_only` exclusion count,
and an uncapped page bound. Never infer the live profile total from `0 new` or
a master-registry creator count.

## Resume after ordinary bounded recovery

Only after `start` has actually ended may continuation be considered. Preserve
the printed handoff path and use read-only `poll` when liveness or eligibility
is uncertain. If it reports `INTERRUPTED`, or it reports `RESUME_READY` after
the non-AI user explicitly requests continuation, run the exact supplied
guarded action. Do not run a separate browser preflight first:

~~~powershell
& $AuditPython $Operator resume --handoff '<absolute-handoff-path>'
~~~

The handoff supplies the immutable database, master database, run ID, source,
count, policy, account, and timeout. Do not restate or change them. The operator
starts/reuses Profile 7 itself and is the only component allowed to spend the
single epoch attempt. For other terminal browser handling, read
[Profile 7 recovery](profile7-recovery.md).

## Native Edge crash and Windows restart

This branch is not part of normal Gemini execution. The current operator has no
trusted channel that can bind a sanitized Windows fault receipt to the exact
Edge PID, execution ID, event time, and boot. It therefore deliberately rejects
every new `prepare-restart` request with `human_action_required`. Gemini must
not invoke that command, manufacture a reason, or tell the user to restart based
on command silence, `collecting`, an unpaired start, a stale event, or generic
error text. Preserve the run and report the terminal blocker for human review.

The following continuation exists only for a legacy or externally trusted
handoff that is already `RESTART_PENDING` and only after the user actually
restarted Windows. Its first browser-touching operation is:

~~~powershell
& $AuditPython $Operator resume `
  --handoff '<absolute-handoff-path>' `
  --after-restart
~~~

The operator independently requires a different OS boot identifier; the flag
and the model's claim are not proof. Only then does it create one new bounded
recovery epoch for the same run. If it still fails, preserve the run and report
the durable blocker; do not loop or create a replacement project.

If a restart was prepared prematurely and Windows did not restart, use the
offline reclassification only after diagnosis establishes no live operator or
collector and no epoch-1 resume:

~~~powershell
& $AuditPython $Operator cancel-restart `
  --handoff '<absolute-handoff-path>' `
  --classification premature_unverified_restart
~~~

The command is narrowly limited to the historical false-claim shape: durable
status is still `collecting`, the latest collection start is unpaired, that run
recorded a successful Profile 7 preflight, Windows is still in the same boot,
no worker/lock is live, and the epoch-1 attempt is unused. It keeps the consumed
restart-handoff count and both retry counters, appends rather than erases the
false claim, and restores epoch 0 only when safe. Its next action is the
ordinary `resume` command without `--after-restart`. A paired terminal browser
failure cannot be cancelled through this path.

## Offline-only operations

These commands do not start or revalidate Profile 7:

~~~powershell
& $AuditPython $Operator poll --handoff '<absolute-handoff-path>'
& $AuditPython $Operator status --handoff '<absolute-handoff-path>'
& $AuditPython $Operator finalize --handoff '<absolute-handoff-path>'
& $AuditPython $Operator validate --handoff '<absolute-handoff-path>'
~~~

`poll` is the only command intended while collection is live and is strictly
read-only. Mutating status/finalize/recovery commands fail while the project
operator lock or a recorded operator/collector process is active. `finalize`
is gated on complete collection and creates or reuses the
project-scoped export before validation. `validate` reads an existing export and
the two databases query-only. The rendered Markdown log is derived from the
machine review; never hand-edit it to claim success.
