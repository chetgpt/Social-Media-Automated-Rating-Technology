---
name: google-3-1-music-audit-instructions
description: >-
  Run or troubleshoot canonical TikTok MUSIC AUDIT collection with
  engage_tiktok.py, including one-post smoke tests, exact topic, creator, or
  URL scopes, Edge Profile 7 verification, same-run resume, project-scoped
  evidence export, and completion checks. Use for MUSIC AUDIT or its legacy
  LISTEN alias; not for PULSE, SONIC AUDIT, AUDIO ARCHIVE, Instagram, AI
  analysis, engagement, or publication.
---

# TikTok MUSIC AUDIT runner

Read the workspace-root AGENTS.md before running anything. It is the authority
when this skill and the workspace differ.

For Gemini/Antigravity execution, a one-post smoke test, or a run that may need
restart-safe continuation, read [Guarded operator](references/operator-wrapper.md)
and use its bundled `scripts/music_audit_operator.py` entry point. The direct CLI examples below document the
underlying canonical commands; do not hand-compose them when the guarded
operator is available.

The guarded `start` and `resume` commands are managed foreground tasks. They
emit sanitized `MUSIC_AUDIT_HEARTBEAT` lines about every 15 seconds while the
canonical collector is alive. A fresh heartbeat, task state `RUNNING`, a live
operator/collector, or a held operator lock is positive evidence to keep
waiting. Durable status `collecting` alone is not process liveness: use the
operator's read-only `poll`. `poll=RUNNING` means wait; `poll=INTERRUPTED` with
no process/lock means ordinary same-handoff resume. Never call `collecting` a
crash. `--browser-startup-timeout 120` is an internal attempt bound, not the
audit's total deadline. Never cancel the task, kill its Python processes, issue
another `start`, change buffering, or run low-level browser/restart recovery in
parallel.

Obey the operator's complete poll table in the guarded-operator reference:
`COLLECTION_COMPLETE_NEEDS_FINALIZE` means offline `finalize`; `COMPLETE` means
stop or offline `validate`; and `BLOCKED` or an exhausted epoch means preserve
and report, not another resume. Process-identity inspection uncertainty fails
closed as `RUNNING`.

## Preserve these invariants

- Route a request to retain or download audio from evidence-ready rows in any
  compatible current, incomplete, copied, moved, restored, or legacy project to
  the separate `audio_archive_tiktok.py` workflow and its
  `docs/contracts/AUDIO_ARCHIVE.md` contract. Do not add an audio flag to the
  guarded MUSIC AUDIT operator or treat a retained-audio request as a LISTEN
  resume. A direct AUDIO ARCHIVE request is sufficient: it may read
  evidence-ready rows from any compatible current, incomplete, copied, moved,
  restored, or legacy project database without a master-registry match or
  extra authorization formula. Use `run_audio_archive_batch.py` for selected
  or all projects. A completed archive covers only its frozen rows; batch
  reruns archive newly eligible rows as bounded deltas and resume only
  identity-matched unfinished archives. Each successful item stores one
  checkpointed M4A+MP3 pair derived from one TikTok acquisition.
- Run the canonical music-audit command. It stores workflow=listen and stops at
  evidence collection; do not start analysis, drafting, approval, or
  publication.
- MusicBrainz is retired. New runs default to an empty catalog-provider set;
  TikTok declarations, contained-recording metadata, and eligible Apple exact-ID
  resolution remain active. Never make a MusicBrainz request or add it to a new
  run. Preserve old evidence/hashes and frozen provider sets. New or repaired
  records in an old run that still names it receive terminal `unsupported` with
  reason `provider_retired`, without network access. See
  `docs/contracts/MUSICBRAINZ_RETIREMENT.md` at the workspace root.
- Use only
  C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe.
- Select exactly one source: topic, exact creator, or one canonical TikTok post
  URL. For a casual "random account, one post" smoke test, use one globally-new
  topic-discovered post from a neutral topic such as music and report the
  creator TikTok returns. Describe it as topic-discovered, not statistically
  random. This procedure has no true random-account selector.
- For a new topic source, use only the normalized topic supplied by the user as
  the TikTok query. Paginate and retry that same query. The requested count,
  candidate reserve, page budget, duplicates, or evidence failures never
  authorize generated prefixes, suffixes, location/language terms, related
  keywords, commercial-intent phrases, or another modifier. If the exact query
  cannot produce the quota, preserve and report the honest
  `collection_incomplete: X/N` result.
- A saved legacy run with immutable
  `topic_query_policy=related_variants_v1` is a compatibility exception only:
  resume its exact existing handoff unchanged. Never opt a new run into that
  policy or migrate a legacy run to `exact` during continuation.
- When the user explicitly requests more than one topic, do not concatenate
  them or send multiple sources to one guarded child. Resolve TOTAL, EACH, or
  CUSTOM quota semantics and route the frozen sequential plan through
  `music_audit_topics.py` as described below. If the count meaning is
  ambiguous, ask rather than guessing.
- Do not pass a TikTok `/music/<slug>-<numeric-id>` sound-detail link to
  `--url`; direct URL means an exact video or photo post. The numeric sound ID
  may be rendered as a derived, query-free locator in a completed review with
  online verification `not_attempted`. Bare `/music/` identifies no exact
  sound. `tiktok_music_page.py inspect-local` is an offline query-only lookup
  over already-known master-registry posts, not a MUSIC AUDIT source or live
  TikTok page resolver.
- For creator `ALL`, the complete guarded argv shape is
  `start --creator '<target>' --all-posts`. Do not append `--posts`,
  `--max-pages`, or another finite discovery bound. `ALL` means an uncapped
  walk to the verified terminal creator frontier; use `--posts N` for a
  bounded creator request.
- Use the existing Edge user-data root with directory Profile 7 in
  existing_profile_attach mode. Let the canonical collector start or reuse it
  first.
- Keep the shared Profile 7 browser running. Never blanket-kill Edge, call
  social_browser.py stop as routine preparation, close the shared browser, or
  substitute another profile, a temporary browser, or the in-app browser.
- Never delete `state.json` or `DevToolsActivePort`, launch Edge with headless,
  direct remote-debugging, `--remote-allow-origins`, or replacement
  `--user-data-dir` flags, create a scheduled-task/batch workaround, or patch a
  launcher or workflow gate.
- Never remove or weaken creator-frontier, exact-count, inventory-hash,
  globally-known-ID, evidence-hash, or stage gates. An honest
  collection_incomplete: X/N result is preferable to fabricated completeness.
- Give a new audit a unique project slug. If a durable run already exists,
  resume that exact run ID; do not delete its state or create a substitute to
  hide a failure.
- For an actual smoke test, add `--run-label test`; for a user-designated brand
  run, use a descriptive label such as `--run-label brand-mie-sedaap`. A label
  changes names only and never changes topic, creator, URL, policy, or count.
  Do not infer `test` from a one-post shape or infer a brand from a creator.
- Keep every guarded output in its canonical dedicated semantic folder
  `comments_data/project_<music_audit_project>/`. New runs use a compact
  hash-bound artifact stem for `state/<stem>_state.sqlite`, `<stem>_handoff.json`,
  `<stem>_ledger.jsonl`, complete-only `<stem>_evidence.jsonl`,
  `<stem>_review.json`, and `<stem>_review.md`. Read the actual paths from the
  handoff or machine review; never assume a fixed basename. Existing v1 runs
  retain their original fixed names and paths. Do not invent `results.json`,
  `audit_results.json`, a project CSV, or a separate export directory. Read the
  guarded-operator reference for the complete artifact tree.
- A blank task log, `Last progress: never`, command silence, or the absence of
  final JSON is not a failure while the managed task remains live. Wait for its
  terminal result. Do not use Antigravity task cancellation, `manage_task
  kill`, `Stop-Process`, or another process action against the operator or
  collector.
- Never print or persist cookies, authorization headers, session tokens,
  signed media URLs, or the CDP WebSocket UUID.

## Explicit multi-topic MUSIC AUDIT

Use the coordinator only after the ordered topics and count semantics are
explicit. Planning is offline and accepts exactly one of these shapes:

~~~powershell
& $AuditPython .\music_audit_topics.py plan `
  --count-mode total --posts 1000 `
  --topic 'mr diy' --topic 'ace hardware'

& $AuditPython .\music_audit_topics.py plan `
  --count-mode each --posts 500 `
  --topic 'mr diy' --topic 'ace hardware'

& $AuditPython .\music_audit_topics.py plan `
  --topic-quota 'mr diy=700' `
  --topic-quota 'ace hardware=300'
~~~

TOTAL assigns `floor(total/topic_count)` to every topic and gives the remainder
to topics in input order; reject a total smaller than the number of topics.
EACH assigns the same positive `--posts N` to every topic. CUSTOM requires a
positive explicit quota for each ordered topic. Reject normalized duplicate
topics. The resulting plan, order, quotas, hashes, and child bindings are
immutable; TOTAL is a fixed allocation, not a fungible shared pool.

Read the exact generated run directory from `plan`, normally beneath
`comments_data/music_audit_topic_runs/`, then use only these commands:

~~~powershell
& $AuditPython .\music_audit_topics.py collect --run-dir '<exact-run-directory>'
& $AuditPython .\music_audit_topics.py continue --run-dir '<exact-run-directory>'
& $AuditPython .\music_audit_topics.py status --run-dir '<exact-run-directory>'
& $AuditPython .\music_audit_topics.py validate --run-dir '<exact-run-directory>'
~~~

`collect` runs one exact-topic canonical guarded MUSIC AUDIT child at a time.
Every child remains `workflow=listen`, `new_only`, full-evidence/music,
Profile-7-gated, and stops without AI or outbound action. Because all children
share the workspace master registry, an overlapping post belongs to the
earliest topic that checkpoints it and later topics must find different IDs.
Never borrow or redistribute a child shortfall. Stop on the first unfinished or
blocked child. `continue` may follow only that exact child's guarded poll and
same-handoff continuation; it must never start a replacement child. `status`
and `validate` are offline. Parent completion requires every fixed child quota
to validate. See `docs/contracts/MULTI_TOPIC_MUSIC_AUDIT.md`.

## Underlying CLI reference — not a Gemini execution path

The commands in this section document the canonical engine underneath the
guarded operator for maintainers. Gemini/Antigravity must not execute these raw
`engage_tiktok.py` examples. It must use the exact guarded `start`, `resume`,
`poll`, `finalize`, and `validate` commands in
[Guarded operator](references/operator-wrapper.md), which add heartbeat,
locking, immutable handoff, duplicate-run protection, and verified export.

Set the workspace and immutable storage paths:

~~~powershell
Set-Location -LiteralPath 'D:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'

$AuditPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Project = "music_audit_$Timestamp"
$ProjectRoot = Join-Path 'comments_data' "project_$Project"
$Database = Join-Path $ProjectRoot 'state\engage_state.sqlite'
$MasterDatabase = 'comments_data\tiktok_master\state\tiktok_master.sqlite'
~~~

For the one-post topic-discovered creator smoke test, use topic discovery:

~~~powershell
$AuditArgs = @(
  '--database'
  $Database
  '--master-database'
  $MasterDatabase
  'music-audit'
  '--project'
  $Project
  '--topic'
  'music'
  '--posts'
  '1'
  '--max-comments'
  '20'
  '--collection-policy'
  'new_only'
  '--browser-startup-timeout'
  '120'
)

& $AuditPython .\engage_tiktok.py @AuditArgs
~~~

Set `$RunId = '<run-id from the emitted JSON>'` immediately after the command,
including when the terminal status is browser_blocked. The project slug and
`project_<slug>` directory are not the run ID. Use the exact emitted/durable
`run_id` value (currently `engage_*`); never invent it from the project name.
Do not add `--json` or another flag absent from the current `music-audit --help`.

For another initial new_only scope, change only the source and cardinality
arguments:

- Topic: --topic '<topic>' --posts N. A new run freezes
  `topic_query_policy=exact` and paginates only that normalized query.
- Creator: --creator '@handle-or-profile-url' --posts N, or
  --creator ... --all-posts only when the user explicitly requests ALL.
- Direct URL: --url '<canonical-video-or-photo-url>' --posts 1.

The creator-ALL form above is exhaustive, not a base command to augment. The
guarded operator rejects `--all-posts` with a positive `--max-pages` even if
current `--help` exposes the flag for other scopes.

Use --expected-account '@handle' only when the operator specified the logged-in
TikTok account. It fences the session account, not the creator whose post is
collected.

## Route REFRESH explicitly

Never let a MUSIC AUDIT REFRESH shortcut fall through to the new_only example.
Keep its exact source and replace --collection-policy new_only with
--collection-policy refresh_known:

- Topic refresh: --topic '<topic>' --posts N. Without an explicit cutoff or
  post ID, candidate selection uses the canonical automatic staleness cutoff.
- Creator refresh: --creator '@handle-or-profile-url' --posts N. ALL is not a
  valid creator-refresh cardinality.
- Direct-URL refresh: --url '<canonical-known-post-url>' --posts 1. Reject an
  unknown post instead of converting it to new_only.
- Repeatable --refresh-post-id '<id>' narrows the immutable registry selection.
  Explicit IDs have no automatic staleness cutoff unless
  --refresh-stale-before '<ISO-8601>' is also supplied.

Both refresh options are invalid under new_only. Resume always reuses the
frozen policy, source, selection, and cutoff from the original run. The
operator hash-binds the complete ordered candidate array after canonical run
creation; never edit or reconstruct that binding.

Record the emitted run_id and terminal status. Do not infer success from a zero
process exit alone: canonical success requires status=collection_complete and
exactly N/N evidence-ready posts.

`requested` is the immutable target and `evidence_ready` is the accepted final
count. `unique_collected` counts every durable unique candidate row, including
failed candidates later replaced, and `failed` counts non-ready history. A
completed topic or creator run may therefore have `unique_collected > requested`
and `failed > 0`. Report those counters without rewriting them. This does not
permit substitution for an exact direct-URL run.

For creator `ALL`, report the verified terminal-inventory fields separately:
`terminal_verified`, `inventory_complete`, `has_more`, frontier stop reason,
unique exact-owner posts observed, frozen inventory count, selected-new count,
and `new_only` exclusion count. `0 new posts` or `0/0` means zero selected new
IDs, not proof of the live profile total. A master-database creator count is
historical coverage and must never be used as exhaustion proof.

## Recover or resume

Use `poll --handoff <path>` only when task liveness or continuation eligibility
is uncertain. Treat its machine-readable state literally:

- `RUNNING`: keep the original managed task alive and wait.
- `INTERRUPTED`: execute the supplied guarded same-handoff action once.
- `RESUME_READY`: a paired terminal browser blocker exists and one continuation
  remains. When the non-AI user explicitly says resume, continue, or try this
  preserved run again, execute `safe_same_handoff_action.argv` directly.
- `BLOCKED`: no guarded continuation is authorized; preserve and report the
  returned reason.

For `INTERRUPTED` or an explicitly user-directed `RESUME_READY`, the guarded
resume is the first and only browser-touching operation. Do not run
`social_browser.py start/status`, a new operator `start`, raw
`engage_tiktok.py resume-collect`, or `--after-restart` first. The guarded
resume owns Profile 7 startup, account verification, immutable run binding, and
the attempt ledger:

~~~powershell
$Operator = '.\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py'
& $AuditPython $Operator resume --handoff '<absolute-handoff-path>'
~~~

Only the handoff's `resume_budget` can prove exhaustion. A low-level browser
failure, Gemini/Antigravity task or server restart, app restart, blank log, or
model claim is not a guarded attempt and is not a Windows restart. Never create
or recommend a fresh same-scope audit instead. Read
[Profile 7 recovery](references/profile7-recovery.md) only for terminal-blocker
handling and the narrow legacy restart rules; it does not add another model-led
preflight loop.

The current guarded operator cannot establish a trusted PID/time-bound Windows
crash receipt and deliberately rejects new `prepare-restart` requests with
`human_action_required`. Preserve and report that outcome; do not request or
infer a restart. A legacy or externally trusted already-pending restart handoff
can continue only after the operator independently verifies a different
Windows boot. A pending restart claim prepared in error may be cancelled only
through the guarded same-boot `cancel-restart` command when all documented
predicates hold; then resume without `--after-restart`.

For a non-browser collection_incomplete result, preserve its evidence and
report the bounded reasons. Resume only when the existing immutable selection
has retryable work. Never substitute posts for a direct URL, bypass a
nonterminal creator frontier, reset the master registry, or repeatedly create
new projects until one happens to pass.

## Export and verify

Export only after collection_complete. Keep the evidence inside the audit's
project directory:

~~~powershell
$Export = Join-Path $ProjectRoot 'music_audit_evidence.jsonl'
$ExportArgs = @(
  '--database'
  $Database
  '--master-database'
  $MasterDatabase
  'export-evidence'
  '--run-id'
  $RunId
  '--file'
  $Export
)

& $AuditPython .\engage_tiktok.py @ExportArgs

$StatusArgs = @(
  '--database'
  $Database
  '--master-database'
  $MasterDatabase
  'status'
  '--run-id'
  $RunId
)

& $AuditPython .\engage_tiktok.py @StatusArgs
~~~

Before reporting success, verify:

- the run is collection_complete: N/N and the export contains exactly N unique
  canonical TikTok post IDs under schema `tiktok-listen-evidence-export-v1`;
- the local workflow run, master-registry run, run-post links, snapshots,
  post IDs, evidence hashes, counters, database paths, and export rows bind
  exactly;
- every counted post is evidence-ready with creator, canonical URL, caption or
  explicit outcome, current metrics, transcript/subtitle outcome, accessible
  comments/replies outcome, evidence provenance, and a valid evidence hash;
- TikTok music declaration and every configured catalog provider have terminal
  outcomes, while acoustic verification and lyrics remain not_attempted;
- no AI report, score, draft, review, approval, authorization, or publication
  artifact was created; and
- the offline run status and export hash validation both succeed without
  starting, attaching to, or revalidating the browser.

Report the project, run ID, source, observed logged-in account, X/N status, post
creator and link, comment coverage, transcript outcome, TikTok music outcome,
catalog outcome, export path, and the browser preflight recorded on the run.
For guarded-operator runs, treat the handoff's `<artifact-stem>_review.json` as
the machine review and `<artifact-stem>_review.md` as its rendered review log;
do not replace either with a freehand diary. Layout-v1 runs retain the old
fixed names. Both forms must report empty AI and outbound action lists.
Leave Profile 7 running; do not touch it merely to validate an offline export.
