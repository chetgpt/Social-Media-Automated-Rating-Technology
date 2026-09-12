# TikTok ENGAGE publication recovery

These rules apply to all TikTok ENGAGE topics, with or without creator mentions.
They do not authorize a comment, a showcase post, CAPTCHA automation, browser
replacement, or recovery of another workflow.

## CAPTCHA and progress

The publisher and native-mention probe check for visible verification challenges.
`human_verification_required` means stop automation and let the operator complete
verification in the preserved Profile 7 tab. The result includes the phase and,
when capture succeeds, a screenshot path. The probe removes its own request fence
and service-worker bypass before leaving a challenge tab available. If it reports
`human_verification_tab_ready=false`, preserve that diagnostic and resolve the
cleanup failure before expecting the challenge to work.

Completing CAPTCHA is not approval of a comment. On continuation, use the same
stored publication if its existing exact-text authorization, evidence freshness,
account and review bindings are still valid. Changed or expired bindings must go
through fresh preparation, review, presentation and authorization. Never extend
timestamps or reuse an approval against different hashes.

`publish_pending.py` supervises one owned Python worker at a time. Safe
`ENGAGE_PUBLICATION_PROGRESS` events show phase, heartbeat and submit/confirmation
flags; durable records live under `comments_data/social_browser/publication_workers/`.
A direct adapter invocation also uses the supervisor. The default worker limits
are 1,200 seconds total and 600 seconds without phase progress, followed by a
10-second cancellation grace. CLI options are `--worker-timeout`,
`--worker-stall-timeout` and `--worker-cancel-grace`. These do not change approval,
freshness or duplicate gates. The adapter additionally bounds the publication page
stage, individual observations and cleanup.
The executable native-mention probe has its own 600-second worker backstop and
never gains publication capability. A blocked or interrupted probe cannot serve
as a successful native-label report.

A deadline cancels only that owned worker. It never closes shared Edge, kills
unrelated Python processes, deletes browser state or automatically submits again.
`reconcile_required` is a request to inspect durable state, not a retry instruction.
Even `--continue-on-error` stops on human-verification or unresolved-submission
states. Never start competing browser diagnosis or publication workers.

## Inspect and resolve an interrupted worker offline

Use the real Python executable:

```powershell
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
& $EngagePython .\engage_publication_recovery.py status `
  --database $Database --publication-id $PublicationId

& $EngagePython .\engage_publication_recovery.py reconcile `
  --database $Database --publication-id $PublicationId
```

Both commands use the run's existing master binding. They do not open TikTok or
publish anything. `status` is read-only; `reconcile` records an interruption only
after the original worker is proven dead by its durable PID and process start time.
Optional `--master-database` must exactly match the stored binding.

| Result/action | Required behavior |
| --- | --- |
| `wait_for_worker` | Preserve the live worker; inspect its progress. |
| `manual_investigation_required` | Worker identity is missing or uncertain, or state needs investigation. Do not invent an owner or clear a reservation. |
| `release_pre_submit` (status) / `gated_retry_available` (reconcile) | No submit intent exists. Reconcile preserves the failed receipt and attempt count; a later explicit publish must pass the normal gates. |
| `preserve_uncertain` / `remote_reconciliation_required` | Submission may have happened. Preserve the fence; do not submit again. This offline command does not prove remote absence or perform remote reconciliation. |
| `confirmed_capture_recovery_only` | Publication is confirmed. Recover screenshot/proof separately using `recover_tiktok_comment_capture.py`. |

Old reservations without a durable owner record cannot be automatically released.
A missing visible comment is not evidence that a previous submission failed.
Confirmed creation is now committed before auxiliary screenshot/reload work, so
interruption there cannot turn the comment into a retryable draft. Published
native-mention persistence and recipient notifications may still be unverified;
report those observations accurately.
Exact-comment capture now checks the confirmed ID before exact rendered text,
bounds the rendered-row scan, and reports specific lookup reasons. It does not
guess a comment, relax text matching or resubmit when a row is missing.

## Refreshing an expired or pre-submit failed exact-text draft

Repeated exact handoff is idempotent and retains attempts, receipt IDs and adapter
state. A different, freshly reviewed and authorized run with identical target/text
must explicitly name the old publication:

```powershell
& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase handoff --run-id $RunId --post-id $PostId `
  --supersedes-publication-id $OldPublicationId
```

This preserves the old row, receipt, analysis and supersession provenance. It is
permitted only for an eligible expired/stale unattempted draft or a master-verified
retryable attempt with no submit intent. Confirmed, active, uncertain and legacy
unbound records remain blocked. Never use SQL `REPLACE`, delete old queue rows,
reset attempt counts or change master history to resolve a collision.

Shared Profile 7 startup is serialized and its UI helper uses one monotonic
deadline across its stages. Startup errors still require their actual diagnostic;
they do not authorize Edge-wide stops, scheduled-task launches, direct-port
workarounds, `DevToolsActivePort` deletion or fabricated verified state.
