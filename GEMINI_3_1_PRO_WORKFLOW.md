# Gemini 3.1 Pro TikTok Workflow Adapter

**Adapter version:** 6.9.1

**Applies to:** Gemini 3.1 Pro / Antigravity agentic execution in this workspace

**Active workflows:** TikTok `PULSE`, `MUSIC AUDIT` (LISTEN-backed),
`MUSIC AUDIT BACKFILL`, `SONIC AUDIT`, `AUDIT`, and `ENGAGE`

This document is a model-specific execution adapter. It is deliberately short
and does not redefine the workspace workflow.

## 1. Authority and conflict resolution

Use this authority order on every task:

1. `AGENTS.md` (binding workspace contract)
2. `WORKFLOWS.md` (current operating guide)
3. The actual command's current `--help` output and database schema
4. This Gemini adapter

If this document conflicts with a higher authority, ignore this document and
follow the higher authority. Do not rely on remembered commands or older chat
examples when current help differs.
CLI help establishes syntax and implemented capability only. It does not grant
permission to add an optional scope, count, page bound, recovery action, or
workflow transition that the user and higher authorities did not request.

For every regular `MUSIC AUDIT` or `LISTEN` request, including `REFRESH` and
browser troubleshooting, load
`.agents/skills/google-3.1-music-audit-instructions/SKILL.md`. Subject to the
authority order above, that skill is the sole model-specific MUSIC AUDIT
execution runbook. Load its Profile 7 recovery reference only after the
canonical collector returns a browser blocker. Generic examples elsewhere in
this adapter do not override the skill, and
`docs/legacy/google 3.1 social browser instructions.md` is retired and non-authoritative.

For `MUSIC AUDIT CREATOR: <target>, ALL`, invoke the guarded operator with the
exhaustive scope `start --creator '<target>' --all-posts`. Do not add `--posts`,
`--max-pages`, or another finite discovery bound. A bounded creator request is
`--posts N`; it is not a smaller interpretation of `ALL`.

The following legacy behaviors are prohibited:

- automated or zero-touch presentation, authorization, handoff, or publication;
- `legacy/one_off_state_mutators/auto_authorize_pipeline.py` as an authorization substitute;
- Edge Default, directory `Profile 1`, managed/temporary profiles, Playwright
  profiles, or the in-app browser;
- a configurable or bare Python command on this workstation;
- legacy `--count`, `--input`, or `--output` ENGAGE examples;
- `social_browser.py stop`, name-wide Edge termination, `taskkill`, deletion of
  browser control state, scheduled-task/browser batch workarounds, or a
  headless/direct-port/replacement-user-data launch;
- direct SQLite writes, fabricated hashes, placeholder evidence, or bypassed
  workflow gates;
- use of `incremental_project.py` or `run_scraper.py` for an active workflow;
- use of `legacy/one_off_state_mutators/enrich_music_data.py` to mutate an exported canonical AI queue;
- cancelling an active MUSIC AUDIT managed task, using `manage_task kill` or
  `Stop-Process` against its operator/collector Python processes, changing
  buffering to force output, or launching a replacement audit because final
  JSON has not appeared;
- treating `--browser-startup-timeout 120` as the whole audit deadline, or
  treating a blank log, `Last progress: never`, heartbeat, task state `RUNNING`,
  held operator lock, or durable `collecting` as a failure; and
- running `social_browser.py`, browser recovery, or `prepare-restart` while the
  guarded operator is still active.

## 2. Fixed workstation bootstrap

At the beginning of every new task, use exactly:

```powershell
$Workspace = 'D:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
Set-Location -LiteralPath $Workspace
```

Never use bare `python`, `py -3`, another Python installation, or another
working directory for workspace commands.

Read `AGENTS.md` and `WORKFLOWS.md` completely before taking workflow action.
Check the relevant command's `--help` before using a command shape that is not
already established by the current task.

## 3. New-project state contract

This section applies to canonical LISTEN, AUDIT, and ENGAGE runs. PULSE is
ephemeral: it creates no project slug, project/master database row, run ID, or
resumable state. Never create or inspect a canonical run merely to execute a
PULSE request.

SONIC AUDIT is durable but isolated from canonical project/master workflow
state. Its runner reads the master registry only to freeze known-post bindings
and writes its own versioned artifacts under
`comments_data/sonic_audit_runs/<run-id>/`; it must never write the master
database or create a canonical LISTEN/AUDIT/ENGAGE run.

A new project creates a new local workflow scope; it does **not** create a new
browser identity. Derive a filesystem-safe project slug and use:

```text
comments_data/project_<slug>/state/engage_state.sqlite
```

Every production collection also attaches the workspace-global master database:

```text
comments_data/tiktok_master/state/tiktok_master.sqlite
```

Never delete, reset, replace, copy over, or edit either database directly.
Before assuming a project database is empty, determine whether it already has
an unfinished run for the same immutable request. Resume that run rather than
creating a duplicate. A blocked command is not permission to create a new run.

If a failed collection response does not expose its run ID, recover the latest
matching run through a canonical read-only status/list facility when available.
If no such facility is available, a read-only SQLite URI (`mode=ro` plus
`PRAGMA query_only=ON`) may be used only to identify the run and counters. Never
write workflow state with raw SQL. Record this recovery as a tooling deviation.
The project slug and `project_<slug>` directory are never a run ID. Preserve the
exact emitted/durable `run_id` field (currently `engage_*`); never derive or
invent it from a project name.

## 4. Mandatory Profile 7 deployment

The only permitted social browser is Microsoft Edge's real user-data root,
directory `Profile 7`, in `existing_profile_attach` mode. Its visible Edge
label may be `Profile 1`; the directory name is the canonical identity.

The first state-changing canonical workflow command must be the applicable
`engage_tiktok.py collect` or `resume-collect`. MUSIC AUDIT BACKFILL instead
uses `music_backfill_tiktok.py run` or `resume`. The applicable command owns
automatic Profile 7 startup or reuse for every work item that revisits TikTok.
For PULSE, `quick_audit_tiktok.py collect` owns the same Profile 7 preflight
without creating canonical workflow state. Do not ask the user to start Edge
first. Do not run a separate low-level browser startup before every project.
For SONIC AUDIT, `sonic_audit_tiktok.py run`, `run-plan-batch`, or `resume` owns
the same preflight before transient media access. Its offline
`validate`/`validate-suite` stages
and its read-only `status` and `export` commands must not attach to Profile 7.
The offline `plan-corpus` utility also must not attach to Profile 7.

For MUSIC AUDIT, invoke the skill's guarded operator as one managed foreground
task and retain that exact task ID until terminal JSON. It emits a sanitized
`MUSIC_AUDIT_HEARTBEAT` about every 15 seconds. Every heartbeat with
`collector_running=true` means keep waiting; do not cancel, start a second
project, or diagnose the browser in parallel. The 120-second browser startup
option bounds an internal attempt, not total wall time; real startup/adoption
may take longer. A separate short task may run only the operator's read-only
`poll --handoff <path>`. If poll says `RUNNING`, the only next action is
`keep_waiting_do_not_restart`. If it says `INTERRUPTED`, use one ordinary
same-handoff resume without low-level browser preparation. If the user has
explicitly requested resume/continue and poll says `RESUME_READY`, execute its
`safe_same_handoff_action.argv` directly as the first browser-touching
operation.

Use the returned state mechanically:

| Operator state | Gemini action |
|---|---|
| `RUNNING` | Retain and wait on the original managed task; optionally poll again later. |
| `INTERRUPTED` | Run the exact guarded `resume --handoff <path>` once, without `--after-restart`. |
| `RESUME_READY` | On an explicit non-AI user resume/continue request, run the returned `safe_same_handoff_action.argv` exactly. Do not preflight separately. Otherwise report that one same-run continuation remains. |
| `COLLECTION_COMPLETE_NEEDS_FINALIZE` | Run guarded offline `finalize --handoff <path>`. |
| `COMPLETE` | Stop, or run guarded offline `validate --handoff <path>`. |
| `RESTART_PENDING` | Stop for human review unless the exact documented legacy false-claim cancellation predicates hold. |
| `BLOCKED` | Preserve and report `next_action`; the guarded ledger authorizes no continuation. Do not start, resume, or infer a restart. |

Process-inspection uncertainty is deliberately treated as `RUNNING`. An
exhausted resume epoch is deliberately `BLOCKED`. Only `resume_budget` from the
guarded handoff can establish exhaustion. A failed low-level command or a
Gemini/Antigravity task, server, or app restart is not a guarded resume attempt
and is not a Windows restart.

Low-level commands are diagnostic only:

```powershell
& $EngagePython .\social_browser.py start
& $EngagePython .\social_browser.py status
```

Never run those commands before `safe_same_handoff_action.argv` or use their
failure to create/propose a fresh same-scope audit. The guarded resume owns
Profile 7 startup and verification.

Do not use `social_browser.py --open-tabs` for this TikTok-only workspace; the
current option may open tabs for other platforms.

### Browser origin and per-command disposition

Keep these two facts separate:

- `bridge_origin` is the historical origin persisted by the current launcher:
  `enabled_here` or `adopted_existing`;
- `invocation_disposition` describes this specific command: `launched`,
  `reused`, `adopted`, or `unknown`.

The current tooling may expose only the historical `bridge_origin`. It does not
always attest whether this specific invocation reused a saved endpoint. When
that fact is unavailable, record `invocation_disposition=unknown` plus a
tooling-observability deviation. Never derive the per-command disposition from
the historical origin, and never say "launched" or "started a new browser"
unless the current invocation independently proves it.

Before TikTok discovery or refresh, the preflight must establish all of these:

1. the local connection is reachable;
2. the attached directory is exactly `Profile 7`;
3. mode is `existing_profile_attach`;
4. TikTok authentication is present;
5. the active TikTok handle resolves; and
6. when an expected account was supplied, the observed handle matches exactly.

The collector should open or reuse exactly one visible TikTok home tab in
Profile 7, perform a bounded page-readiness/reload retry, and keep Profile 7
running. It must never close the browser or user-owned tabs. If the current
tool closes the only useful TikTok tab or cannot expose a visible intervention
tab, report `tooling_noncompliance` rather than pretending that the browser was
visibly deployed.

If the handle still cannot resolve after the automatic retry:

- preserve the existing run and database;
- leave a useful TikTok tab open when the tool supports it;
- report the real run ID, absolute database path, durable status and counters,
  available bridge origin, per-command disposition or `unknown`, browser-check
  identifier, and sanitized error;
- stop before all AI stages;
- ask for human browser interaction only at this point; and
- resume the same run after correction.

Never fall back to another browser/profile, lower the requested count, or create
a replacement run.

Never call `social_browser.py stop`, broadly terminate Edge, delete
`state.json` or `DevToolsActivePort`, add `--headless`, direct remote-debugging,
`--remote-allow-origins`, or replacement `--user-data-dir` flags, create a
scheduled-task launch workaround, or patch a launcher or workflow gate. The
canonical collector or same-run resume owns Profile 7 startup.

Only a finished canonical attempt with a paired terminal result and sanitized
Windows process/WER evidence newer than the current boot and time-/PID-bound to
that exact attempt may show that Edge itself crashed. A stale newest event,
command silence, `collecting`, an unpaired start, a model-selected reason, or
self-caused browser unreachability is not evidence. Stop script-level CDP
experimentation and preserve the same run. The current guarded operator has no
trusted PID/time-bound fault-receipt channel and rejects every new
`prepare-restart` request with `human_action_required`. Gemini must not invoke
it, fabricate its evidence, or tell the user to restart from model-visible
diagnostics. Only a legacy or externally trusted already-pending handoff may use
`resume --after-restart`, after the user actually restarted Windows and the
operator independently verifies a changed boot identifier. Do not pre-run
low-level browser start/stop. A restart never permits a replacement run or an
unbounded retry loop.

Profile 7 is accessed only for collection/refresh, live music backfill work,
authorized SONIC AUDIT media access, and immediately before an authorized
outbound TikTok action. Backfill `status`
and `export` use stored state and must not attach to the browser. PULSE analysis uses the already emitted
ephemeral packet; canonical analysis, AUDIT aggregation, drafting, review,
storage, presentation, and authorization use local state. None of those AI or
local stages may start, attach to, or revalidate the browser.

## 5. Workflow routing and stopping boundaries

Parse the user's shortcut according to `AGENTS.md`. When ambiguous, choose
`ENGAGE SHADOW` and perform no outbound action.

Match `SONIC AUDIT CREATOR` before the shorter string `AUDIT`. Match
`MUSIC AUDIT BACKFILL` (including CREATOR and URL variants) before
`MUSIC AUDIT REFRESH`, then match all MUSIC AUDIT forms before the shorter
strings `AUDIT` or `QUICK AUDIT`. A regular MUSIC AUDIT creates the internal
`workflow=listen` and stops at `collection_complete`. Backfill creates its own
durable music-maintenance run. Neither creates AI scores or a
`tiktok-audit-report-v1` report.

### PULSE / QUICK AUDIT

```text
browser preflight -> one shallow bounded sample -> one built-in AI batch
-> deterministic noncanonical quick signals -> display -> discard -> stop
```

Route `QUICK AUDIT` to PULSE. Use only `quick_audit_tiktok.py`; do not create a
canonical run or database. PULSE accepts a positive topic/creator sample or one
direct post URL. It rejects `ALL`, refresh, resume, deep exact-count expansion,
and every response/publication operation. An `X/N` sample is a valid visibly
partial result. Metrics-only rows must be `unrated`, never guessed.

### MUSIC AUDIT (`workflow=listen`)

```text
browser preflight -> exact topic/creator/URL collection or refresh
-> terminal TikTok music extraction
-> deterministic configured-catalog enrichment
-> hash-bound evidence storage and master synchronization -> stop
```

Route `MUSIC AUDIT`, `MUSIC AUDIT CREATOR`, `MUSIC AUDIT URL`, and their
REFRESH forms through the canonical LISTEN engine. MUSIC AUDIT is the official
user-facing name; `listen` remains the immutable database/CLI workflow value.
It accepts a topic, exact creator, or one exact canonical TikTok video/photo
URL. Direct URL requires `--posts 1` and may never search for or substitute
another post. MUSIC AUDIT cannot export or import AI
analysis and cannot draft or publish. Its only export command is the read-only
`export-evidence` handoff for a completed MUSIC AUDIT run; that command does not
advance the workflow or invoke AI.

MusicBrainz lookup is collector-owned deterministic evidence enrichment, not a
Gemini/Antigravity semantic stage. Do not independently Google the song, invoke
Spotify/Shazam/acoustic recognition/lyrics services, interpret mood or lyrics,
or edit an exported queue. Inspect and report only the stored canonical
declaration, provider outcome, candidates, provenance, and hashes. A catalog
correlation is not acoustic verification, and a generic `original sound` label
is not proof that the uploader created the audible recording.

### MUSIC AUDIT BACKFILL

```text
select and freeze eligible known master-registry posts plus base hashes
-> Profile 7/account preflight
-> fetch TikTok music metadata only
-> deterministic Apple tt2dsp and MusicBrainz enrichment when eligible
-> append hash-bound music observations -> stop
```

When the user asks to apply MUSIC AUDIT to posts collected previously, route to
`music_backfill_tiktok.py`; do not default to a full `refresh_known` collection.
Backfill selects known topic, creator, URL, or explicit-ID rows whose latest
music evidence is missing, older than the target schema, or in a selected
retryable state. `ALL` means every eligible row in the frozen registry
selection, not all live posts on the creator's current profile.

Backfill is not `new_only`: it never makes a post new. It is not
`refresh_known`: it must not refetch metrics, transcripts, subtitles, comments,
replies, or visual evidence, append a full evidence snapshot/delta, or update
master `last_seen`. It freezes each post ID, canonical URL, base evidence
snapshot identity/hash, selection order, target schema, providers, and retry
rules, then appends a separate music observation with its own timestamp and
hash. An explicit terminal `unavailable` result is allowed for inaccessible or
metadata-missing posts.

For topic, URL, and explicit-ID work items, direct HTML remains primary. Only a
failed frozen URL or explicit-ID target may invoke the narrow fallback: group
failures by their frozen owner, inventory each affected exact creator once,
consume only a row whose ID, owner, and media type match the frozen binding,
and ignore unrelated rows. Never use this fallback for topic scope, broaden the
selection, or substitute a post. Terminal profile absence becomes
`unavailable`; when a still-missing target has only a nonterminal frontier,
leave the run incomplete and resumable because absence was not proven. This is
music-only transport: do not fetch metrics, comments, transcripts/subtitles,
audio, or invoke Gemini/another AI. The CLI surface is unchanged.

Backfill remains collection-only maintenance. It cannot invoke Gemini for
semantic analysis, score or rate content, infer acoustic or lyrical facts,
draft/review responses, authorize, or publish. `status` and `export` are local
read-only operations and must not touch Profile 7 or providers.

### SONIC AUDIT

Before proposing another audio run, Gemini may use `plan-corpus` as a narrow
offline, query-only exception to v1's executable creator-only scope. It accepts
exactly one repeatable source: `--creator` or exact `--post-id`.
`--exclude-run-id` is repeatable only with creator scope; every referenced run
must be complete, fall within the requested creator set, and bind the same
master database. The required planning options are `--min-repeated-groups`,
`--min-positive-pairs`, `--max-posts`, `--max-posts-per-reference`, and
`--file`.

The planner accepts only hash-verified public-video labels from exact Apple
`apple_itunes_lookup` `tt2dsp` resolutions in completed, current v3 music-
backfill evidence. It deterministically produces creator-local batches of at
most 60 posts and writes a no-clobber
`tiktok-sonic-audit-corpus-plan-v1` artifact that binds scope, thresholds,
candidate/evidence hashes, and every exclusion ID/hash. It performs no browser,
TikTok, provider, media/audio, AI, master mutation, or SONIC run creation.
Explicit post-ID scope is all-or-nothing. A plan is not transient-audio
authorization and never self-executes.

`run-plan-batch` is the sole bridge from one planned batch to an executable
SONIC run. It requires a fresh non-AI human authorization for that exact plan
and batch; no plan, prior permission, or other batch authorizes it. Before any
browser access or run creation, validate the complete plan/source/set hashes,
every candidate/group/batch hash and relationship, the requested batch and
master path, and every current master snapshot, evidence, v3 music observation,
exact Apple `tt2dsp` resolution, and latest-observation binding. Fail closed on
any drift or tampering.

The selected batch must remain exactly one creator, 1-60 ordered unique public
videos, with no discovery or substitution. Preserve its typed Apple track
reference and plan/batch/group/candidate provenance through feature records.
Reject a second run for the same plan hash and batch ID; resume the existing
run. A supplied expected account is frozen. Bounded transient-media cleanup,
no built-in semantic AI, read-only-master, and `exploratory_only` rules remain
unchanged.

```text
verify explicit per-run transient-audio authorization
-> read/freeze N known exact-owner master rows and base evidence hashes
-> Profile 7/account preflight
-> bounded transient media acquisition and local audio decode
-> deterministic local fingerprints/features and comparisons
-> optionally preflight and upload that same frozen audio to Mirelo under its
   separate authorization/credit ceiling, then derive a sanitized symbolic
   diagnostic
-> delete all raw media/audio/intermediates
-> store isolated derived checkpoints/evaluation and verify cleanup -> stop
```

Route only `SONIC AUDIT CREATOR: <handle>, <N>` with `1 <= N <= 60` to an ad
hoc `run` in `sonic_audit_tiktok.py`. The planner and its exact guarded batch
bridge are the only exceptions. Ad hoc v1 rejects topic, URL, explicit-ID,
`ALL`, live creator
inventory, new collection, and post substitution. It reads the master registry
without writing it and freezes exact ordered post IDs, canonical URLs, creator,
base snapshot identities/hashes, selection method, count, master path, and
authorization. If fewer than `N` valid known exact-owner
rows exist, stop with the real error; do not reduce `N` or search replacements.
Each completed record separately hash-binds its effective feature schema,
algorithm, and configuration; its transport receipt binds fixed v1 conversion
provenance and timing. Resume only under the unchanged deployed code,
dependencies, and configuration. Never mix post records produced after an
implementation change into an older run.

Do not execute a new run or planned batch until a non-AI user explicitly
permits transient media/audio acquisition for that specific selection. A plan,
MUSIC AUDIT request, backfill request, past permission, another batch's
permission, or Gemini's assessment of usefulness is not authorization. Only
after permission is present may Gemini pass
`--authorize-transient-audio`; the flag records the human decision and never
creates it. Resume relies on the frozen authorization and cannot change scope.

Mirelo Audio-to-MIDI is optional and disabled by default. Gemini may enable it
only while creating a new ad hoc `run` or exact `run-plan-batch`, and only when
the non-AI user separately authorizes upload of that exact frozen run and
attests that they hold the rights needed under the current
[Mirelo Terms](https://mirelo.ai/terms). The complete flag gate is
`--authorize-transient-audio`, `--mirelo-audio-to-midi`,
`--authorize-mirelo-upload`, and `--mirelo-max-credits N`, with a positive
immutable run-wide credit ceiling. Local-audio permission, a corpus plan, permission for
another run/batch, a configured key, or Gemini's recommendation is not Mirelo
upload authorization. Gemini must tell the user before authorization that
provider-side input/output assets may remain at Mirelo for up to 24 hours.

The adapter reads its secret only from the process environment variable
`MIRELO_API_KEY`; Gemini must never request that it be pasted into chat or put
it in a command, plan, manifest, repository file, database, log, error,
checkpoint, or export. Before each exact frozen-post upload, require Mirelo's
credit/ETA preflight and keep committed plus estimated credits within the
frozen ceiling. If the key, preflight, rights/authorization, or budget gate is
missing, stop the provider branch without uploading. Do not discover,
substitute, or broaden the run to make Mirelo succeed. Resume uses the frozen
provider/model, permission, ceiling, and candidate set; do not restate or
change Mirelo flags on `resume`.

Provider audio, MIDI, MusicXML, raw/structured notes, instrument tracks,
payloads, and job/result/download URLs are transient. Persist only a sanitized,
non-reconstructable, hash-bound symbolic summary with provider/model/config,
input-audio hash binding, terminal outcome, safe preflight/credit scalars,
aggregate diagnostics, timings, and result hashes. Mirelo's probabilistic
output is separate supporting evidence: it must not alter the primary
`recording_score`, thresholds, catalog identity, or cluster ground truth, and
cannot support identity, genre, mood, lyrics, ownership, creator-quality, or
engagement-causality claims. It is not an acoustic-recognition shortcut.
`status` and `export` stay offline and must not call Mirelo, use the key, follow
a provider URL, or spend credits. Provider tests are mocked only with synthetic
fixtures—never live uploads or credits. Follow the current
[Audio-to-MIDI API docs](https://mirelo.ai/api-docs#audio-to-midi) and
[model documentation](https://mirelo.ai/models/audio-to-midi).

SONIC AUDIT performs local deterministic signal processing, not per-post Gemini
analysis. It cannot interpret lyrics, invent semantic sound labels, score a
creator or content portfolio, make causal engagement claims, draft/review a
response, authorize, or publish. Similarity does not identify a catalog item.
Display a track/artist only when independently corroborated by hash-bound music
evidence on the frozen base snapshot, retaining its provenance and uncertainty.
V1 uses its checked-in deterministic DSP fingerprint/feature baseline. Do not
download or substitute Essentia, CLAP, Shazam, AcoustID lookup, another model,
or an external recognition service. The separately gated Mirelo branch is a
symbolic diagnostic, not catalog recognition or a replacement baseline.

### AUDIT

```text
browser preflight -> exact collection -> built-in AI analysis of every post
-> deterministic hash-bound report -> audit_complete -> stop
```

AUDIT cannot reclassify responses, draft, review responses, present, authorize,
handoff, or publish.

### ENGAGE SHADOW

```text
browser preflight -> exact collection -> built-in AI analysis
-> response classification -> built-in AI drafting
-> independent built-in AI critic -> reviewed response storage -> stop
```

SHADOW cannot present, authorize, hand off, or publish.

### ENGAGE LIVE

Live intent does not authorize publication. The exact stored response must be
shown to a named non-AI human with `show-response`. That same human must approve
the exact presentation-bound text using the one-time token. Only then may the
normal publication preflight, handoff, sequential publication, receipt, and
separate COMMENT SHOWCASE process occur. Never infer approval and never expose
an approval token in an attempt log.

## 6. Current command patterns

Use current CLI help as the final authority for syntax. It does not authorize
optional arguments absent from the user's request. These are shape examples,
not hardcoded project values.

### PULSE quick snapshot

```powershell
& $EngagePython .\quick_audit_tiktok.py collect `
  --topic "3D printing" --posts 5

& $EngagePython .\quick_audit_tiktok.py collect `
  --creator "@maker" --posts 5

& $EngagePython .\quick_audit_tiktok.py collect `
  --url "https://www.tiktok.com/@maker/video/1234567890"
```

The command returns `snapshot` and `analysis_input` on standard output. Analyze
all sampled posts once with built-in Gemini/Antigravity—never an external LLM
API—and pass an ephemeral object containing `snapshot` and `analyses` to:

```powershell
$PulseBundleJson | & $EngagePython .\quick_audit_tiktok.py report `
  --actor antigravity-gemini31-pulse
```

`$PulseBundleJson` is serialized JSON text, not a formatted PowerShell object.
Do not write the packet to a project queue/database or invoke Profile 7 again
during analysis/reporting. Present the mandatory noncanonical/ephemeral banner,
counts, denominators, links, omissions, confidence, quick signals, and
limitations; then discard the packet. Never call PULSE `audit_complete` or use
its output in AUDIT/ENGAGE.

### New AUDIT or ENGAGE topic collection

```powershell
& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  collect `
  --project $Project `
  --topic $Topic `
  --posts $RequestedCount `
  --workflow engage `
  --collection-policy new_only `
  --mode shadow
```

Use `--workflow audit` for AUDIT. Creator collection uses `--creator` instead
of `--topic`; creator `ALL` uses `--all-posts` instead of `--posts`. MUSIC
AUDIT does not use this generic example; use the dedicated skill and canonical
`music-audit` subcommand.

### MUSIC AUDIT direct URL

Collect one globally-new exact post:

```powershell
$Operator = '.\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py'
& $EngagePython $Operator start `
  --url "https://www.tiktok.com/@maker/video/1234567890" `
  --posts 1 `
  --max-comments 100 `
  --collection-policy new_only `
  --browser-startup-timeout 120
```

Refresh that exact known post:

```powershell
& $EngagePython $Operator start `
  --url "https://www.tiktok.com/@maker/video/1234567890" `
  --posts 1 `
  --max-comments 100 `
  --collection-policy refresh_known `
  --browser-startup-timeout 120
```

Do not encode a direct URL in `--topic`. Under `new_only`, an already-known URL
finishes `collection_incomplete: 0/1` rather than selecting a replacement.
Under `refresh_known`, the URL-derived post ID is the immutable explicit
selection, has no automatic 24-hour cutoff unless one was supplied, and must
already exist in the master registry. `--all-posts` and a `--posts` value other
than `1` are invalid. Do not use `--url` with AUDIT or ENGAGE.

Every LISTEN result must show a terminal TikTok music declaration and one
terminal outcome for every provider frozen at run creation. Initial
MusicBrainz outcomes are `matched`, `ambiguous`, `not_found`, `unsupported`,
`unavailable`, `rate_limited`, and `provider_error`. `matched` maps to
`identity_status=catalog_correlated`; it is not acoustic verification.
`not_found` is reserved for a successful no-candidate result, and the provider
failure statuses must remain distinct. Preserve up to five structured
candidates, provider/result hashes, cache-hit/circuit provenance, and separate
post/music durations. Preserve
`acoustic_verification.status=not_attempted`, `verified=false`, and
`lyrics.status=not_attempted`. Never convert missing support into a fabricated
identity.

For `original sound`, inspect only the stored structured
`platform_contained_recording` outcome. It is populated from TikTok
`music.matched_song`/`matchedSong`, falling back to
`matched_pgc_sound`/`matchedPgcSound`. When its title and artist are complete,
the collector—not Gemini—uses it for MusicBrainz and stores
`input_basis=platform_contained_recording`. Do not infer a contained song from
UI text or comments, and never call this acoustic verification. Safe `tt2dsp`
IDs retain the TikTok declaration as `partial`; the collector may separately
resolve a validated platform `1` Apple ID through the declared Indonesian
Apple lookup adapter. Report the hash-bound result as `tt2dsp_resolution`, and
when resolved expect MusicBrainz `input_basis=tt2dsp_catalog_resolution`.
Never call Apple/Spotify independently, promote a platform `3` ID to complete
metadata, or retain preview/artwork/embed/raw-response fields.
Do not translate or guess a localized original-sound label. The collector uses
TikTok's structured `music.original`/`isOriginal` flag as the primary generic
wrapper signal and uses bounded title matching only as a fallback.

Do not call MusicBrainz independently or in parallel. The collector reserves
the workspace-global provider slot in the master registry and enforces a
1.05-second minimum start interval across concurrent projects. A recorded
in-memory cache hit is not a new provider request. A `rate_limited` result
extends the shared cooldown from `Retry-After`, or a conservative fallback.
The collector also reserves `apple_itunes_lookup` starts workspace-wide with a
3.05-second minimum interval.

Omitting `--music-catalog` selects the current default `musicbrainz`. Supplying
`--music-catalog musicbrainz` makes the same immutable run setting explicit.

A MUSIC AUDIT run cannot be promoted or analyzed in place. A later AUDIT must create
its own canonical AUDIT or AUDIT REFRESH run. When that run collects or
refreshes the same post, use only the hash-bound music declaration, catalog
candidates, terminal outcomes, and provider/result hashes present in its AI
projection. Preserve uncertainty, make no unsupported acoustic or lyrical
claim, and do not infer that music caused engagement.

### SONIC AUDIT commands

Corpus planning requires no transient-audio authorization because it neither
accesses nor executes audio:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  --output-root $SonicRoot `
  plan-corpus `
  --creator $Creator `
  --exclude-run-id $PriorCompleteRun `
  --min-repeated-groups $MinRepeatedGroups `
  --min-positive-pairs $MinPositivePairs `
  --max-posts $MaxPosts `
  --max-posts-per-reference $MaxPostsPerReference `
  --file $CorpusPlan
```

Repeat `--creator`, or use repeatable `--post-id` instead. Never combine the
two scope types or attach `--exclude-run-id` to post-ID scope. Do not interpret
the resulting plan as permission or silently translate its batches into runs.

After fresh human authorization for one exact batch, use:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  --output-root $SonicRoot `
  run-plan-batch `
  --project $Project `
  --plan-file $CorpusPlan `
  --batch-id $BatchId `
  --authorize-transient-audio `
  --expected-account $ExpectedAccount
```

Only after the separate rights/upload authorization described above, append
the following Mirelo options to that `run-plan-batch` command or to the ad hoc
`run` command below:

```powershell
--mirelo-audio-to-midi `
--authorize-mirelo-upload `
--mirelo-max-credits $MireloMaxCredits
```

They supplement, never replace, `--authorize-transient-audio`. Never add them
to another command or infer either authorization.

Omit `--expected-account` only when no exact account was requested. Never edit
the plan to make it pass. The runner must verify every closed plan, scope, set,
batch, group, and candidate hash plus all current master/music/Apple bindings
before Profile 7 or run creation. It freezes the one-creator, maximum-60-post
batch in exact order and preserves typed Apple and selection provenance. A
duplicate plan-hash/batch-ID run is rejected; use the reported run ID with
ordinary `resume`, `status`, `validate`, `validate-suite`, and `export`.
Plan-bound results remain exploratory.

Before executing a SONIC run, confirm that the user explicitly authorized
transient media/audio for this new run. For the authorized 60-post BankBCA
pilot, use this exact v1 shape:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  run `
  --project bankbca_sonic_pilot `
  --creator "@bankbca" `
  --posts 60 `
  --authorize-transient-audio
```

For this 60-post pilot, expect the deterministic selector to target 21 usages
from repeated exact Apple references, 30 distinct singleton Apple references,
and 9 unresolved/original posts. A short bucket is filled only from remaining
eligible frozen exact-owner pools, without duplicates or live replacements.
Report the realized bucket counts from the manifest rather than assuming the
target mix was available.

Do not translate the request to `engage_tiktok.py`, MUSIC AUDIT BACKFILL, or a
legacy downloader. Do not add a topic, URL, ID list, `ALL`, model argument, or
catalog-provider argument. `--authorize-transient-audio` means permission has
already been obtained; never infer it or add it speculatively.
`run`, `run-plan-batch`, and `resume` may use `--expected-account` only when an
exact active Profile 7 account was specified. It is independent of the target
creator; `run-plan-batch` freezes it when supplied. Omit the global
`--output-root` for production so the required default artifact root is used.

For a separate nonoverlapping labelled-first extension, add repeatable
`--exclude-run-id $PriorCompleteRun` to `run`. Each excluded run must be
complete and bind the same creator/master database. Its exclusion hashes are
frozen, the new run remains capped at 60 posts, and new explicit transient-
audio authorization is required.

Resume, inspect, and export only the same immutable run:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  resume --run-id $RunId

& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  status --run-id $RunId

& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  validate --run-id $RunId

& $EngagePython .\sonic_audit_tiktok.py `
  --output-root $SonicRoot `
  validate-suite --run-id $RunA --run-id $RunB --file $SuiteJson

& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  export --run-id $RunId --file $SonicAuditJson
```

Run `validate` only against a completed immutable SONIC AUDIT run. It needs no
new transient-audio authorization and must not attach to Profile 7, access
TikTok or media/audio, invoke Gemini or another AI, call a provider, or write
the master database. It verifies and hash-binds the manifest, source report,
and ordered feature-record set, leaves `report.json` and `state.json` unchanged,
and writes only
`comments_data/sonic_audit_runs/<run-id>/statistical_validation.json`. The
artifact uses wrapper schema
`tiktok-sonic-audit-statistical-validation-v1` and nested evaluation schema
`sonic-statistical-validation-v1`.

Interpret its full-sample positive/negative score distributions and threshold
sweep as descriptive only. Threshold calibration and evaluation are
reference-label-disjoint and out of fold; uncertainty output includes a
reference-grouped bootstrap plus Wilson intervals explicitly marked as
finite-support diagnostics. The current BankBCA support is eight repeated
reference IDs and ten positive pairs, so report
`recommendation_status=exploratory_only` and never promote it to a production
threshold. `status` and `export` remain read-only, and export includes the
statistical-validation artifact when present.

`validate-suite` requires at least two ordered unique complete runs with no
post overlap and identical creator, master binding, and feature contract. It
binds all source hashes and the combined feature-set hash, then creates the
requested `tiktok-sonic-audit-statistical-validation-suite-v1` artifact without
clobbering an existing file. It must not access Profile 7, media/audio, the
master registry, or AI. Report the combined result as `exploratory_only`.

`run`/`run-plan-batch`/`resume` use Profile 7 for only the frozen URLs and
validate each post ID and creator. Acquisition and decode are bounded. Raw
video, audio, PCM, stems,
and intermediate spectrograms are deleted through cleanup paths after each item
and on failure/cancellation. Never log or persist signed URLs, cookies, headers,
tokens, or raw payloads. Before claiming completion, verify the run reports zero
persistent raw-media files under both its temporary location and
`comments_data/sonic_audit_runs/<run-id>/`.

The sequential v1 transport defaults are 64 MiB source media, 8 MiB decoded
WAV, 180 seconds per post, and 3,600 seconds per run. Absolute caps are 128 MiB
source, 16 MiB WAV, and 300 seconds per post. It also caps HTML at 8 MiB,
requests at 30 seconds, transcode at 90 seconds, and redirects at three. Do not
raise those hard limits to make a post succeed; report the terminal bound
outcome and verify cleanup.

Map safe per-item transport failures to `unavailable`, continue the frozen set,
and never substitute. Preflight/account, callback/programming, or run-wide
budget errors leave pending items and `sonic_incomplete` for resume. Report
`sonic_complete` only when all selected items have immutable `completed` or
`unavailable` terminal records.

Only manifest/bindings, terminal checkpoints, decoded-audio hashes, safe
duration/quality scalars, fingerprints, numeric features/embeddings,
similarity/cluster results, thresholds, timings, software/model provenance,
evaluation, and result hashes may persist. `status` and `export` must not attach
to Profile 7, access TikTok, recompute features, invoke Gemini, or write run/
master state. Verify the export against stored hashes and report its resolved
path and record count. The export envelope schema is
`tiktok-sonic-audit-export-v1`; it includes the hash-verified statistical-
validation artifact when one exists.
The report wraps `sonic-feature-v1`, `sonic-similarity-report-v1`, and
`sonic-cluster-stability-v1` outputs under `tiktok-sonic-audit-report-v1`.

For the pilot report, provide selected/processed/terminal coverage, completed/
unavailable/pending counts, and any run error; total and per-stage/per-post
timing; cleanup verification; similarity/cluster coverage and abstention;
feature/schema hashes; and cluster stability. Only when independent positive
and negative same-recording labels are sufficient may you report post-disjoint
labelled/distinct/repeated reference support, evaluable/resolved/unresolved
query counts, abstention rate, Recall@1, Recall@5, pair TP/FP/TN/FN, precision,
true-positive rate, and false-match rate at the selected threshold. Otherwise
emit `not_evaluable`. Report cluster stability separately as mean/minimum/
maximum adjusted Rand index with its resampling basis. Never use within-post
windows as separate test posts or present cluster cohesion as identity accuracy.
Do not estimate accuracy or runtime before execution; report
only the measurements present in a completed hash-verified pilot report.
Read per-item `html_fetch_ms`, `media_download_ms`, `inspect_transcode_ms`,
`processor_ms`, and `total_item_ms`, plus run `preflight_ms`, `attach_ms`,
`transport_ms`, and `total_run_ms`; all are monotonic measured durations.

### MUSIC AUDIT BACKFILL commands

Use the exact current CLI surface. The master database is a global option and
must precede the subcommand:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run `
  --project $Project `
  --creator "@maker" `
  --all-eligible `
  --target-schema tiktok-music-evidence-v3
```

For a positive bound, replace `--all-eligible` with `--limit $RequestedCount`.
Replace `--creator` with exactly one of `--topic`, `--url`, or repeatable
`--post-id` values. For example:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run --project $Project `
  --post-id 1234567890 --post-id 1234567891 `
  --all-eligible
```

URL and post-ID targets must already exist in the master registry. A missing
target is an error, not permission to create a `new_only` run. Optional `run`
arguments are repeatable `--retry-status`, `--force`, `--expected-account`, and
`--max-pages`. Do not infer a retry status or force current records merely to
increase the selected count. Do not invent a fallback flag: the existing runner
automatically tries direct HTML first and applies the exact-target recovery
rules above only when eligible.

Resume, inspect, or export the same frozen run:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  resume --run-id $RunId

& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  status --run-id $RunId

& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  export --run-id $RunId --file $MusicBackfillJsonl
```

`resume` may add only operational `--expected-account` and `--max-pages`; it
must not restate or change the scope, cardinality, selection, target schema,
retry rules, providers, or base-snapshot bindings. Report terminal TikTok,
Apple resolution, and MusicBrainz statuses separately. Do not summarize every
nonmatch as `not_found`: a generic original-sound declaration is normally
`unsupported`, and provider failures retain their distinct status.

### MUSIC AUDIT evidence export

For a completed MUSIC AUDIT run, use the exact current CLI shape:

```powershell
& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  export-evidence `
  --run-id $RunId `
  --file $EvidenceExportJsonl
```

`--database` is global and must appear before `export-evidence`. Do not add
browser options: this subcommand reads only the already stored local evidence
and must never start, attach to, or revalidate Profile 7. It also invokes no
built-in or external AI.

The tool must reject any non-LISTEN run, any status other than
`collection_complete`, an active collection attempt, an exact-count mismatch,
an invalid creator inventory, invalid stored evidence JSON/hash, a post-ID
binding mismatch, or a row not marked evidence-ready. On success it atomically
writes one `tiktok-listen-evidence-export-v1` JSONL row per requested post with
run/project/source/policy identity, post ID, canonical evidence hash,
projection hash, and the safe semantic `evidence_packet`.

The packet retains caption and metrics, transcript/subtitle outcomes and
segments, compact comments/replies, availability/provenance, and sanitized
TikTok music plus MusicBrainz result/candidates, identity status, catalog result
hash, and music evidence hash. It omits avatars and signed
media/share/audio/artwork transport URLs. Treat the file as evidence only: do
not describe it as an AI queue, AUDIT promotion, approval, or publication
eligibility.

After the command, verify its returned record count and resolved artifact path;
the attempt ledger may also calculate the file's SHA-256 without modifying it.
The export file is the sole intended write; run status, counters, checkpoints,
project/master workflow state, and browser state must remain unchanged.

### Resume and status

For MUSIC AUDIT, use only the guarded handoff. `poll` is the nonmutating live
progress command; `resume` is the one browser-touching continuation. These are
alternative routes, not a two-command sequence:

```powershell
$Operator = '.\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py'
# Route A: use only when liveness or eligibility is uncertain.
& $EngagePython $Operator poll --handoff '<absolute-handoff-path>'

# Route B: on explicit user direction and RESUME_READY/INTERRUPTED, run this
# directly as the first browser-touching operation.
& $EngagePython $Operator resume --handoff '<absolute-handoff-path>'
```

When poll supplies `safe_same_handoff_action.argv`, execute that array without
rewriting its handoff, adding `--after-restart`, starting the browser
separately, or creating a new project. Keep the managed task alive through its
heartbeats and wait for terminal JSON.

The raw commands below are underlying/non-MUSIC-AUDIT workflow reference only.
Gemini must not use them to run or resume MUSIC AUDIT because they bypass its
heartbeat, lock, immutable operator handoff, and machine review.

```powershell
& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  resume-collect --run-id $RunId --browser-startup-timeout 120

& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  status --run-id $RunId
```

Resume reuses the saved immutable source mode/target, count, browser/account
binding, collection policy, catalog-provider set, refresh selection, and master
database. Do not restate different run settings on resume.

### Built-in AI queue operations

For canonical modes, use only canonical queue export/import commands with
`--run-id` and `--file`.
Every import also supplies the required `--actor`. Do not use `--input` or
`--output`.

Recommended Gemini actor identifiers:

```text
antigravity-gemini31-analysis
antigravity-gemini31-drafter
antigravity-gemini31-critic
```

The critic must operate in a genuinely separate context and its actor must
differ from the drafter. If independent review is unavailable, stop rather
than self-approve. Built-in Gemini/Antigravity performs semantic work from the
stored queue; no external LLM API or placeholder-generating script may replace
it. Tool-owned hashes and canonical imports remain authoritative.

Do not advance when collection is `X/N`. Exact `N/N` evidence-ready records are
required. For a creator `ALL` run, the verified terminal inventory determines
the frozen requested count.

Completion is governed by `status=collection_complete` and
`evidence_ready=requested`. `unique_collected` counts every durable unique
candidate row, including failed candidates later replaced, so it may exceed
`requested`; `failed` may likewise remain nonzero. Report all counters without
rewriting them. This never permits substitution for an exact direct-URL run.

## 7. Observable attempt ledger

The durable attempt-ledger requirements below apply to canonical workflow
evaluation. PULSE's default contract is ephemeral, so do not create a
`comments_data/model_attempts` directory or persist its extracted packet,
analyses, or scores. For PULSE, report only a sanitized observable command
summary in chat. If a human explicitly requests an evaluation log, record
command metadata only and keep the TikTok packet/results out of it.

For model evaluation, create a sanitized attempt directory under:

```text
comments_data/model_attempts/<UTC_TIMESTAMP>_gemini31/
```

The model-authored diary is secondary evidence. The workflow database, master
database, canonical artifacts, events, browser checks, and publication receipts
remain authoritative.

For MUSIC AUDIT, use the guarded operator bundled with its skill. It writes the
project-scoped hash-chained `<artifact-stem>_ledger.jsonl`, machine-readable
`<artifact-stem>_review.json`, and rendered `<artifact-stem>_review.md` from
authoritative status, local/master bindings, and the verified export. Read the
exact stem and paths from the guarded handoff or machine review; do not replace
those artifacts with a freehand narrative log.

The guarded operator creates one dedicated
`comments_data/project_music_audit_[<label>_]<new|refresh>_<source>_<target>_<scope>_<timestamp>_<microseconds>/`
directory per run. Add `--run-label test` only for an actual test and a label
such as `--run-label brand-mie-sedaap` only when the user designated that
purpose. The label affects names only; never use it to change or replace the
real topic, creator, URL, policy, or cardinality. Keep
`state/<artifact-stem>_state.sqlite`, the stemmed handoff, ledger, reviews, and
complete-only `<artifact-stem>_evidence.jsonl` in that folder. Existing
layout-v1 runs stay at their original fixed paths and must not be renamed. Do
not create generic `results.json`, `audit_results.json`, project CSVs, or a
separate export folder. The shared master database remains at
`comments_data/tiktok_master/state/tiktok_master.sqlite`.

Append one JSONL record before and after every command/tool action. Use a
strictly increasing sequence beginning at `1`. Record observable facts, not
private chain-of-thought:

- UTC timestamp, stage, action, and concise policy basis;
- exact sanitized absolute interpreter, working directory, and argv;
- command start/end, exit code, duration, and sanitized error;
- real run ID, post ID where applicable, database path, and event/check IDs;
- status and counters before and after the action;
- available bridge origin, per-command disposition or `unknown`, and resolved
  account result;
- input/output artifact paths, record counts, and SHA-256 hashes;
- per-post TikTok music status, frozen catalog-provider set, provider terminal
  outcomes, candidate counts, provider/result hashes, and cache provenance;
- command-level retries separately from internal browser/page retries;
- deviations, blockers, and the next permitted stage; and
- publication/navigation actions attempted and completed as explicit lists.

Never record cookies, authorization headers, CDP/WebSocket URLs or UUIDs,
access tokens, approval tokens, signed media/audio/artwork URLs, catalog
provider credentials, `msToken`, signatures, or environment secret values. Use
`<redacted>` when a field cannot safely be omitted.

After every durable stage, run the canonical status command and record the
authoritative returned state. Do not claim success from narrative output alone.
On failure, include the failure itself in the ledger and preserve all durable
state.

The final summary must include:

```text
executor_compliance: PASS | FAIL
task_outcome: COMPLETE | BLOCKED
run_id
absolute database path
durable status and all counters
bridge origin, per-command disposition or unknown, and account verification
result
artifact manifest with hashes
deviations and retries
outbound actions attempted/completed
final stopping reason
```

For creator `ALL`, also include cardinality, terminal verification,
`inventory_complete`, `has_more`, frontier stop reason, unique exact-owner
inventory count, selected-new count, `new_only` exclusions, and whether the
discovery page bound was uncapped. `0 new` and master-registry counts are not
live-profile exhaustion proof.

Model/provider/version identity in this summary is self-claimed unless the
runtime supplies an independent attestation.

## 8. Completion checklist

Before declaring a task complete, verify:

- the required Python interpreter and workspace were used;
- only verified Edge directory `Profile 7` was used;
- PULSE, when requested, used only the shallow ephemeral runner, displayed its
  noncanonical banner and coverage, left no project/master run, and stopped
  without any outbound action;
- bridge origin and per-command disposition were reported without inference;
- for canonical modes, the local and master databases contain the same run and
  counters and exact collection reached its immutable requested count;
- no AI stage touched Profile 7;
- LISTEN, AUDIT, and SHADOW stopped at their required boundaries;
- MUSIC AUDIT BACKFILL, when requested, used only
  `music_backfill_tiktok.py`, froze known-post/base-hash bindings, appended
  music-only observations, and did not refresh full evidence, update
  `last_seen`, invoke AI, or create publication state;
- SONIC AUDIT, when requested, had explicit permission for that run, used only
  `sonic_audit_tiktok.py`, froze exactly the requested known exact-owner posts
  and base hashes, kept the master read-only, persisted only derived artifacts,
  verified zero raw media after cleanup, invoked no built-in per-post semantic
  AI, and created no canonical analysis, approval, or publication state;
- optional Mirelo work, when requested, had separate exact-run upload/rights
  authorization and a frozen credit ceiling, used only `MIRELO_API_KEY`, passed
  preflight before each upload, stayed within the exact frozen set and budget,
  disclosed up-to-24-hour provider retention, persisted only sanitized hash-
  bound symbolic summaries, left `recording_score` unchanged, and made no
  identity/genre/mood/lyrics/rights/quality/causality claim;
- SONIC `plan-corpus`, when requested, used exactly one repeatable creator or
  post-ID scope, read only verified completed v3 Apple-resolved backfill
  evidence, wrote one no-clobber hash-bound plan, touched no browser/media/AI or
  master state, created no run, and granted no audio authorization;
- SONIC `run-plan-batch`, when requested, had fresh permission for that exact
  batch, validated every plan/current-registry binding before browser access,
  froze one creator and at most 60 ordered candidates without substitution,
  preserved typed Apple and hash provenance, created no duplicate batch run,
  kept media transient/master read-only, invoked no built-in semantic AI, and
  remained exploratory;
- a requested LISTEN `export-evidence` ran only after `collection_complete`,
  exported the exact hash-verified semantic row count, did not touch Profile 7
  or AI, and left workflow state unchanged;
- every counted LISTEN record has terminal TikTok music and configured-catalog
  outcomes, with post duration kept separate from declared music duration;
- catalog correlation was not reported as acoustic verification, and no lyrics,
  music-fit, sentiment, or causal claim was created during LISTEN;
- `legacy/one_off_state_mutators/enrich_music_data.py` or another post-hash queue mutation was not used;
- ENGAGE drafting and review actors were independent;
- no direct SQL writes or fabricated hashes occurred;
- no presentation, authorization, handoff, publication, or showcase action was
  inferred; and
- no secret appeared in logs or chat.

A platform or browser blockage may produce compliant execution with a blocked
task outcome. Never claim completion without the authoritative artifacts.
