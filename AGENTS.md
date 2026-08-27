# TikTok ENGAGE Workspace Contract

This workspace is operated as a TikTok-only evidence and engagement project
with three canonical durable workflow modes: `LISTEN` collects evidence,
`AUDIT` collects and analyzes evidence, and `ENGAGE` continues through response
work. `MUSIC AUDIT` is the official user-facing name for the music-enriched
LISTEN profile: it uses the durable `workflow=listen` engine to collect a
complete evidence pull with terminal TikTok music-declaration and configured-
catalog outcomes, then stops. `PULSE` (also
called `QUICK AUDIT`) is a separate noncanonical, ephemeral quick-look mode.
`MUSIC AUDIT BACKFILL` is a separate durable maintenance workflow for upgrading
music evidence on posts that already exist in the workspace-global master
registry. It appends music-only observations and never turns a known post into
a new collection record.
`SONIC AUDIT` is a separate permission-gated research workflow for comparing
the audible signal of already-known creator posts. It may acquire and decode
bounded media transiently only after explicit authorization for that run,
persists derived hashes/features/results in an isolated run directory, and
deletes raw media/audio on both success and failure. It does not relax or
change MUSIC AUDIT's metadata-only rules.
Canonical LISTEN, AUDIT, ENGAGE, PULSE, BACKFILL, and SONIC execution remains
TikTok-only. A separate platform-neutral, metadata-only social-music substrate
may normalize and durably import already collected Instagram, Facebook, X, and
YouTube records through `social_music_audit.py`. Its evidence contract lives in
`tiktok_scraper/social_music_contract.py`, and its collection-only state lives
in `tiktok_scraper/social_music_state.py`. This substrate is not a publication
path, does not promote the legacy multiplatform runner into a canonical
workflow, and does not create a cross-platform chat shortcut by itself. Live
platform adapters must be separately implemented, permission-scoped, and
enabled before a social MUSIC AUDIT may claim that it collected platform data.
See `docs/contracts/CROSS_PLATFORM_MUSIC_AUDIT.md` for the authority and capability limits.
Its current `supported` and `unsupported` values describe offline importer
acceptance only; they are not claims of official-API availability, authenticated
field provenance, or live-adapter authorization.

A separate `linkedin_workflow.py` collector may use LinkedIn's official API for
authorized organization-Page evidence. It has isolated state and a closed
collection-only capability contract described in `docs/contracts/LINKEDIN_WORKFLOW.md`. It
does not make LinkedIn a canonical TikTok workflow target, extend the
platform-neutral social-music importer, or change any TikTok shortcut, browser,
analysis, engagement, or publication rule below.

A separate `tiktok_one_top_content.py` runner may use the authenticated TikTok
One **Discover top content** page only as a read-only link-discovery source.
Its exact source is `https://ads.tiktok.com/creative/forpartners/creator/top-content`
with at most one normalized `region` value. It uses the required existing Edge
Profile 7 session, captures the filters already selected without changing them,
and traverses the observed Top 100 for video views, engagement, then 6-second
views. Requests may exceed 100 by continuing across those rankings and
deduplicating numeric video IDs. Success requires exactly the requested number
of query-free canonical TikTok video URLs; otherwise store
`links_incomplete: X/N` with bounded reasons.

Top Content discovery persists only URL identity, ordinal/rank/filter
provenance, terminal status, and integrity hashes in its isolated database. It
must never persist TikTok One captions, comments/replies, media, images, HTML,
raw responses, cookies, headers, tokens, or signed URLs, and it must never click
Invite, bookmark, contact, message, or another outbound control. A completed
link run may fan out sequentially through the existing
`engage_tiktok.py music-audit --url <url> --posts 1` command. Each child remains
an ordinary canonical direct-URL LISTEN run with the global `new_only` fence,
complete evidence/music collection, and no AI or publication. Known master IDs
are skipped and reported separately. This discovery runner adds no canonical
source mode or chat shortcut. See `docs/contracts/TIKTOK_ONE_TOP_CONTENT.md`.

Treat the shortcuts and gates below as persistent instructions in every task
opened from this workspace.

## Chat Shortcuts

- `MUSIC AUDIT: <topic>, <post amount>` is the official name for a complete
  topic evidence pull containing posts, captions, metrics, transcript/subtitle
  outcomes, accessible comments/replies, TikTok-declared music metadata, and
  configured catalog support such as MusicBrainz. It is stored underneath as
  `workflow=listen` and stops after evidence storage; it performs no semantic
  AI scoring, drafting, approval, or publication.
- `MUSIC AUDIT CREATOR: <@handle or profile URL>, <post amount or ALL>` uses
  that exact creator as the source under the same complete music-supported
  evidence contract.
- `MUSIC AUDIT URL: <canonical TikTok video or photo URL>` collects exactly
  that one post under the same contract and never substitutes another post.
- `MUSIC AUDIT REFRESH: <topic>, <post amount>`,
  `MUSIC AUDIT CREATOR REFRESH: <@handle or profile URL>, <post amount>`, and
  `MUSIC AUDIT URL REFRESH: <canonical TikTok video or photo URL>` are the
  corresponding `refresh_known` forms.
- `MUSIC AUDIT BACKFILL: <topic>, <post amount or ALL>` selects eligible known
  topic posts whose latest music evidence is missing, older than the target
  schema, or has an explicitly retryable terminal outcome. It freezes the
  eligible selection and appends music-only observations; it does not perform a
  full evidence refresh.
- `MUSIC AUDIT CREATOR BACKFILL: <@handle or profile URL>, <post amount or
  ALL>` applies the same music-only upgrade to eligible known posts owned by
  that exact creator. `ALL` means all eligible master-registry rows in the
  frozen selection, not every live post on the creator's current profile.
- `MUSIC AUDIT URL BACKFILL: <canonical TikTok video or photo URL>` selects
  that exact known master-registry post. Explicit TikTok post IDs may also be
  supplied to the CLI as a repeatable exact scope. Unknown URLs or IDs are
  rejected rather than collected as new posts.
- Parse every `MUSIC AUDIT BACKFILL` shortcut before `MUSIC AUDIT REFRESH`,
  `AUDIT`, or `QUICK AUDIT`. Backfill is durable collection-only maintenance:
  it performs no semantic AI scoring, drafting, review, approval, or
  publication.
- Parse every `MUSIC AUDIT` shortcut before matching `AUDIT` or `QUICK AUDIT`.
  Regular MUSIC AUDIT and its REFRESH forms mean stored `workflow=listen`, stop
  at `collection_complete`, and never create AI scores or a
  `tiktok-audit-report-v1` report. BACKFILL uses its separate maintenance state
  and the same no-AI boundary defined below.
- `SONIC AUDIT CREATOR: <@handle or profile URL>, <1-60 posts>`
  selects exactly that many already-known public video posts owned by the exact
  creator, freezes their IDs and base evidence hashes, computes local
  acoustic fingerprints/features from transient audio, evaluates the frozen
  set, stores only safe derived artifacts, and stops. Initial v1 ad hoc `run`
  accepts only this finite creator scope; topic, URL, explicit-ID, `ALL`,
  discovery, and substitution are invalid. The offline `plan-corpus` utility
  and its guarded `run-plan-batch` bridge are defined below; neither is another
  chat shortcut.
- Parse `SONIC AUDIT CREATOR` before the shorter string `AUDIT`. A MUSIC AUDIT,
  backfill, audit, or earlier media permission never authorizes SONIC AUDIT.
  Before `run` or `run-plan-batch`, a non-AI user must explicitly authorize
  transient media/audio acquisition for the specific run or batch;
  `--authorize-transient-audio` records that authorization and may not be
  supplied on the model's own initiative.
- `LISTEN: <topic>, <post amount>` or `gather data for <post amount> posts`
  remains the lower-level/legacy chat alias for `MUSIC AUDIT`. It uses the same
  complete music-supported collection contract and stopping boundary.
- `LISTEN CREATOR: <@handle or profile URL>, <post amount or ALL>` uses the
  exact creator profile as the discovery source. It inventories that creator's
  public TikTok posts, stores evidence for the selected new posts, and stops
  before analysis.
- `LISTEN URL: <canonical TikTok video or photo URL>` creates a durable
  one-post collection-only run. It binds that exact post ID, owner, media type,
  and canonical URL, collects and music-enriches its evidence, stores it, and
  stops. It never searches for or substitutes another post.
- `LISTEN REFRESH: <topic>, <post amount>` means collection-only incremental
  refresh of known posts. Use `refresh_known`, append new evidence snapshots
  and deltas, and stop before analysis. Explicit post IDs may be supplied
  instead of a topic selection.
- `LISTEN CREATOR REFRESH: <@handle or profile URL>, <post amount>` refreshes a
  fixed known exact-owner set under `refresh_known`, appends observations and
  deltas, and stops before analysis.
- `LISTEN URL REFRESH: <canonical TikTok video or photo URL>` refreshes that
  exact known post under `refresh_known`, appends its new observation and
  delta, and stops before analysis.
- `AUDIT: <topic>, <post amount>` collects exactly the requested globally-new
  evidence set, analyzes it with the built-in AI, stores a deterministic
  provisional portfolio report, and stops. It never drafts or publishes a
  response.
- `AUDIT CREATOR: <@handle or profile URL>, <post amount or ALL>` replaces
  topic discovery with the exact creator profile. It analyzes the selected
  fixed count or every globally-new post in the verified terminal profile
  inventory, stores the provisional creator-content report, and stops.
- `AUDIT REFRESH: <topic>, <post amount>` uses `refresh_known` for a fixed
  number of stale known topic posts, analyzes the appended evidence snapshots,
  stores a new report for that immutable refreshed set, and stops. Explicit
  post IDs may be supplied for a narrower fixed selection.
- `AUDIT CREATOR REFRESH: <@handle or profile URL>, <post amount>` uses
  `refresh_known` for a fixed number of stale known posts owned by that exact
  creator, analyzes the new observations, stores the creator report, and
  stops. It never clears or changes comment-publication history.
- `PULSE: <topic>, <positive sample amount>` or
  `QUICK AUDIT: <topic>, <positive sample amount>` runs one shallow topic
  discovery pass, analyzes the sample with the interactive built-in AI,
  displays noncanonical quick signals, discards the packet, and stops.
- `PULSE CREATOR: <@handle or profile URL>, <positive sample amount>` takes one
  finite profile-order sample. It never accepts `ALL`, inventories the full
  profile, or claims a creator rating.
- `PULSE URL: <canonical TikTok video or photo URL>` analyzes one shallow
  direct-post observation. It is still noncanonical and ephemeral.
- `ENGAGE: <topic>, <post amount>` defaults to `ENGAGE SHADOW`.
- `ENGAGE CREATOR: <@handle or profile URL>, <post amount or ALL>` defaults to
  `ENGAGE SHADOW` and replaces topic search with exact-owner profile
  collection. `ALL` means every globally-new, publicly accessible post in the
  run's verified terminal profile snapshot, not a coded maximum.
- `ENGAGE CREATOR REFRESH: <@handle or profile URL>, <post amount>` selects
  stale known posts belonging to that creator, refreshes them directly, then
  follows the normal SHADOW stages. It never clears the already-commented
  ledger. Explicit post IDs may be used for a narrower refresh.
- `ENGAGE REFRESH: <topic>, <post amount>` uses `refresh_known`, then follows
  the normal SHADOW analysis, drafting, review, and storage sequence. Refresh
  never clears or bypasses the already-commented ledger.
- `ENGAGE SHADOW: <topic>, <post amount>` runs collection, built-in AI
  analysis, drafting, independent AI review, and storage. It cannot publish.
- `ENGAGE LIVE: <publication id or stored draft>` requests guarded publication
  of a specific final response. It still requires `show-response` to present
  the exact rendered response to a named human, a valid one-time presentation
  token, that human's explicit approval, and all publication-time checks.
- `ENGAGE NO-API: <topic>, <post amount>` uses the interactive built-in AI for
  analysis, drafting, and review instead of an external LLM API. `NO-API` is
  not a bypass: it follows every collection, review, storage, presentation,
  authorization, and publication gate in this file.
- `COMMENT SHOWCASE: <publication id, showcase id, or all pending>` resumes
  the TikTok-only secondary-post workflow for confirmed comments. It may
  recover a missing exact-comment screenshot, prepare media, draft and review
  captions with built-in AI, and present the exact outputs. It cannot infer
  authorization from the original comment approval and must stop for separate
  named-human approval before API publication.

If a request is ambiguous, choose `ENGAGE SHADOW`, preserve the collected data,
and perform no outbound TikTok action.

## Required Python Interpreter

On this workstation, run active workspace scripts with
`C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe`. Do not use
bare `python` from the workspace and do not use `py -3`; the launcher is not
registered for this installation. Models must resolve and invoke the real
interpreter before browser preflight instead of treating an interpreter error
as a browser failure.

## Separate LinkedIn Official-API Collection Contract

`linkedin_workflow.py` is the only active LinkedIn collection entry point. Its
stored workflow values are `listen` and `engage`, but both currently mean
`collection_only`; `publication_enabled=false` and
`external_ai_enabled=false`. Unqualified chat shortcuts such as `LISTEN:` and
`ENGAGE:` remain TikTok-only. Do not infer a LinkedIn shortcut or route a
TikTok run, database, master registry, browser session, AI stage, draft,
approval, or publication through this collector.

LinkedIn collection uses only `LINKEDIN_ACCESS_TOKEN` from the process
environment and LinkedIn's official API. Never pass or persist the token in a
CLI argument, repository file, database, output, or log. It must not start,
attach to, or scrape Profile 7 or any other browser.

Each run accepts exactly one authorized organization-bound source:

- `--source organization` inventories one administered Page identified by an
  exact `urn:li:organization:<numeric-id>` and requires a positive finite
  `--posts N`;
- `--source post` accepts one exact authorized organization-authored
  `urn:li:share:...` or `urn:li:ugcPost:...`, requires that Page's exact
  organization URN, and requires `--posts 1`.

Topic/hashtag discovery, arbitrary members or member feeds, person-authored
posts, `ALL`, URLs as source authority, browser scraping, and substitutions are
unsupported. The official API must verify that every hydrated post belongs to
the frozen organization; reject an owner mismatch before checkpointing.

Collection is exact-count and `new_only` inside the isolated LinkedIn database.
For an organization source, exclude Page-post URNs already known there before
freezing the ordered eligible inventory plus its hash. An exact-post source
freezes its one requested URN, but the known-post fence prevents hydration or
counting and never substitutes another post. Checkpoint only matching inventory
URNs and ordinals. Succeed only at exactly `N` evidence-ready records; otherwise
store `collection_incomplete: X/N`. `resume --run-id` may reopen an incomplete
run only to continue or repair that same immutable inventory, count,
organization, source, API version, and comment cap. It must not rediscover,
reorder, add, remove, substitute, or reopen a complete run.

LinkedIn member comment/reply payloads expire after 48 hours. Purge their text,
actors, metrics, and any hashes while retaining only resource-URN deletion
tombstones and purge time. Organization-post payloads expire after 180 days;
scrub their body, content, metrics, collection data, URL/timestamps, and
evidence hash while retaining only the URN identity needed for tombstones and
the `new_only` fence. The isolated default database is
`comments_data/linkedin/linkedin_collection.sqlite3`; it must never be merged
with TikTok master/workflow state or the social-music database.

LinkedIn `LISTEN` and `ENGAGE` stop after collection storage. They expose no
export, AI analysis, drafting, review, approval, authorization, or publication
command. Use the required Python interpreter above for the exact CLI examples
in `docs/contracts/LINKEDIN_WORKFLOW.md`.

## Mandatory Social-Browser Preflight

The only valid social browser is Microsoft Edge's existing user-data root with
profile directory `Profile 7`, attached in `existing_profile_attach` mode. Its
visible Edge label may be `Profile 1`; the directory `Profile 7` is the
canonical identity. This profile intentionally contains logged-in sessions for
multiple social networks. That is expected; TikTok ENGAGE uses only its TikTok
session and verifies the active TikTok handle.

Before TikTok discovery, live collection, evidence refresh, music-backfill
metadata access, SONIC AUDIT media access, or any outbound TikTok action:

1. The executing model must start or reuse Profile 7 itself. The
   `engage_tiktok.py collect` command performs this automatically for canonical
   runs; `music_backfill_tiktok.py run` and `resume` own it for any backfill
   work item that must revisit TikTok; `quick_audit_tiktok.py collect` owns the
   same preflight for PULSE; and `sonic_audit_tiktok.py run`,
   `run-plan-batch`, and `resume` own it for SONIC AUDIT work that revisits
   TikTok. For
   diagnosis, the equivalent low-level commands are
   `<required-python> social_browser.py start` and
   `<required-python> social_browser.py status`.
2. Verify that the local debugging connection is reachable, the connected
   profile is exactly Profile 7, TikTok is logged in, and the active TikTok
   handle can be resolved. When an expected account is specified, require an
   exact match; otherwise bind the resolved handle to the run.
3. Keep that shared Profile 7 session running for the workflow. Workers may
   open and close temporary tabs but never close the browser.

For a guarded MUSIC AUDIT, a fresh `MUSIC_AUDIT_HEARTBEAT`, managed-task state
`RUNNING`, a held operator lock, or a live operator/collector process means the
existing command is still in progress. Keep that exact task alive and wait. A
blank task log, `Last progress: never`, or the absence of final JSON is not a
browser failure while one of those liveness signals remains. Durable status
`collecting` alone is nonterminal but does not prove that a process survived;
use the guarded read-only `poll`. If it reports `RUNNING`, wait. If it reports
`INTERRUPTED` with no process or lock, preserve and ordinarily resume the same
handoff without browser diagnosis. If it reports `RESUME_READY`, a paired
terminal browser blocker exists and the current recovery epoch still has one
guarded same-handoff continuation. When the non-AI user explicitly asks to
resume or continue that run, execute the returned
`safe_same_handoff_action.argv` directly as the first browser-touching
operation. Do not precede it with `social_browser.py start`,
`social_browser.py status`, a new guarded `start`, or `--after-restart`.
Without that explicit user direction, preserve the run and report that the
continuation remains available. Never classify `collecting` as a crash. The
operator's other states are also literal:
`COLLECTION_COMPLETE_NEEDS_FINALIZE` means offline `finalize`, `COMPLETE` means
stop or offline `validate`, and `BLOCKED` means the operator did not authorize
another same-run continuation (including an exhausted resume epoch), so
preserve and report. Only the guarded handoff/ledger attempt budget can prove
exhaustion; a standalone browser command, model/tool/server restart, or failed
diagnostic cannot. Process-inspection uncertainty is treated as `RUNNING`. The
`--browser-startup-timeout` value bounds an internal browser attempt; it is not
the total wall-clock deadline for collection, and startup/adoption retries may
make preflight take longer than that value. Never kill the operator or
collector Python process, start a replacement audit, change buffering, or run
low-level browser diagnosis/recovery in parallel. The guarded operator's
read-only `poll` command may be used from another task without touching the
browser or mutating operator/workflow state. Browser diagnosis/recovery begins only after the
original command has ended with a structured terminal blocker. Ordinary
same-handoff continuation after `poll=INTERRUPTED` is not browser recovery and
must not be preceded by low-level browser commands. Explicit user-directed
continuation after `poll=RESUME_READY` follows the same direct guarded-resume
route. Never create, propose, or recommend a fresh same-scope project as a
substitute for the preserved run.

Built-in AI analysis, AUDIT report generation, drafting, independent review,
response storage, `show-response`, and authorization operate only on the
already collected packet or stored evidence. They must not start, attach to, or
revalidate Profile 7. PULSE performs its immediate analysis from the emitted
in-memory packet after its one preflight; canonical modes use local workflow
state. Revalidate only when TikTok is actually accessed: during collection or
an evidence refresh, and immediately before publication.

Never ask the user merely to start Edge: first attempt the automatic Profile 7
startup/reuse path. Never substitute Edge Default, a managed or temporary
profile, Playwright's new profile, or the in-app browser. If startup fails,
diagnose or report the launcher failure without falling back. Ask the user to
interact with the browser only after Profile 7 is verified running and its
TikTok session is actually logged out, its account navigation cannot be
resolved after retry, or its active TikTok handle differs from the expected
handle. Any login or account switch must happen inside that same Profile 7
window.

Never use `social_browser.py stop`, a name-wide Edge kill, browser-control
state deletion, headless/direct-port/replacement-user-data flags, a scheduled
task workaround, or a launcher/gate patch to recover an active workflow. If
sanitized process and Windows fault evidence, time- and PID-bound to the exact
finished collection attempt and newer than the current boot, confirms that
Edge itself crashed before a stable visible Profile 7 window can exist,
preserve the exact run and stop script-level CDP experimentation. Command
silence, `collecting`, a model-supplied reason, a stale Windows event, or
self-caused browser unreachability is never crash evidence. A registered Edge
repair or Windows restart requires explicit operator participation. The current
guarded MUSIC AUDIT operator has no trusted PID/time-bound Windows fault-receipt
channel and therefore rejects every new model-issued `prepare-restart` request
with `human_action_required`. A model must preserve and report that outcome,
not fabricate evidence or tell the user to restart. Only a legacy or externally
trusted already-pending handoff may continue after an actual user restart; its
first browser-touching action is one canonical same-run resume, and the operator
must independently observe a different Windows boot identifier before consuming
the new recovery epoch. Do not pre-run low-level browser start/stop. A restart
never creates a new run or an unlimited retry loop.

Repeat the connection, login, and exact account checks immediately before
publication. Never print or persist cookie values, authorization headers, or
session tokens; ENGAGE browser credentials remain in memory only.

## Exact Collection Contract

This exact-count and persistence contract applies to canonical `LISTEN`,
`AUDIT`, and `ENGAGE` runs. PULSE follows the separate best-effort sampling
contract below and must never represent its `X/N` sample as canonical evidence.

A request for `N` posts succeeds only when exactly `N` unique TikTok post IDs
have evidence-ready records. Search results, duplicate URLs, inaccessible
posts, and partial records do not count toward `N`.

`N` is any positive requested count; `50` is an example, not a coded maximum.
Discovery, storage, AI queues, authorization, and sequential publication must
derive their bounds from the requested run and must not contain a hidden
50-post ceiling. For a very large request, genuine source exhaustion,
platform refusal, or a resource bound explicitly requested by the non-AI user
may still produce `collection_incomplete: X/N`, but the executing model must
never select or add that bound itself and the workflow must never silently
truncate the request to 50.

Canonical LISTEN accepts exactly one source mode: topic, creator, or direct
URL. Direct URL is a separate source mode, not a topic string disguised as
search. It accepts one canonical TikTok video or photo URL, requires
`--posts 1`, freezes the URL, post ID, owner, and media type, and has immutable
cardinality `1`. It never accepts `ALL`, related discovery, or replacement
hydration. Initial direct-URL support is LISTEN-only; it does not add an AUDIT,
ENGAGE, or PULSE execution path.

Creator collection is a separate source mode, not a topic string disguised as
search. The target creator and the logged-in Profile 7 TikTok account have
different roles: the target creator owns the posts being collected, while the
logged-in account is the account that may later publish authorized comments.
Do not require those handles to match unless the operator explicitly selected
the same account for both roles.

Before hydrating a creator-profile run, enumerate the exact profile through the
authenticated TikTok profile-post feed. Bind the canonical handle and stable
creator identity, reject rows owned by another creator, preserve both video and
photo URLs, and deduplicate pinned/repeated cards by canonical post ID. A
profile inventory is complete only after TikTok returns a verified terminal
frontier (`hasMore=false`). A page cap, pagination stall, access refusal, or
unverified owner makes the inventory incomplete and keeps all AI stages locked.
For creator `ALL`, the executing model must omit `--max-pages` and every other
finite discovery bound. The guarded MUSIC AUDIT shape is exhaustively
`start --creator <target> --all-posts`; a bounded creator request must use
`--posts N` instead. Reaching a cap before `hasMore=false` is
`collection_incomplete`, never a smaller successful `ALL` inventory.

For fixed creator cardinality, exactly `N` globally-new evidence-ready posts
must still be checkpointed. For `ALL`, first freeze the terminal inventory,
remove globally known IDs under `new_only`, store the selected ordered ID set
and inventory hash, then derive the run's requested count from that frozen set.
An empty verified profile may therefore complete as `0/0`; an unresolved
profile may not. Resume reuses the frozen inventory and cannot add, remove,
reorder, or substitute posts after terminal freeze.

`0 new posts`, `requested=0`, or `evidence_ready=0` proves only that the
terminal frozen inventory contained no globally-new eligible IDs. It does not
by itself prove the creator's current profile total or exhaustion. A
master-registry count by creator is historical known coverage, not a live
profile inventory. Report a current observed total only from the verified
terminal inventory and include its terminal flag, `hasMore`, stop reason,
unique exact-owner count, selected-new count, and `new_only` exclusion count.

Creator `refresh_known` is selected from the master registry by normalized
creator handle and opens the saved canonical URLs directly. An ENGAGE creator
refresh must exclude posts blocked by confirmed or unresolved prior comment
publication state for the active account. Refresh appends new observations and
does not change the immutable new-only inventory or publication history.
An AUDIT creator refresh is observational and may include a known post whether
or not it has comment-publication history; it never changes or bypasses that
history and cannot create a new comment-publication attempt.

Every production `collect` and `resume-collect` command attaches the fixed
workspace-global TikTok master database in addition to the per-project
workflow database. The default `new_only` collection policy excludes globally
known TikTok post IDs before metadata, transcript, or comment hydration.
Excluded IDs do not count toward `N`; discovery must expand until `N` new
evidence-ready IDs are checkpointed or a real frontier is exhausted.

`refresh_known` is a separate explicit collection policy. At run creation it
selects stale master-registry candidates, then stores the exact candidate IDs,
candidate records, and absolute staleness cutoff as immutable run settings.
It refreshes those canonical URLs directly through TikTok HTML metadata and
the comments/transcript collector; it must not rediscover them through broad
search. A failed HTML metadata refresh remains partial and cannot reuse cached
caption or metrics as fresh evidence.

For direct-URL LISTEN under `new_only`, the one exact post counts only when its
ID is globally new. If that ID is already known, record
`collection_incomplete: 0/1` with the known-post reason and do not substitute a
different post. For direct-URL LISTEN under `refresh_known`, derive the exact
post ID from the URL and use it as the immutable explicit registry selection.
It receives no automatic 24-hour cutoff unless the operator supplies
`--refresh-stale-before`. Reject an unknown refresh target rather than silently
changing it into a new-only run.

Use repeatable `--refresh-post-id <id>` options for an explicit targeted
refresh. Explicit IDs are passed to the registry selection and are immutable
on resume. They do not receive the automatic 24-hour staleness cutoff unless
`--refresh-stale-before` was also supplied. Both refresh options are invalid
under `new_only`.

Every evidence-ready local collection checkpoint appends its evidence
snapshot/delta to the master registry; partial checkpoints remain local for
repair or replacement. Short-lived collection leases prevent concurrent tasks
from hydrating the same candidate. The per-project database remains
authoritative for exact-count status, evidence hashes, AI stages,
authorization, and publication receipts. Resume reuses the saved policy,
source target, catalog-provider set, selection, staleness cutoff, and
master-database path.

Every guarded MUSIC AUDIT run owns exactly one dedicated output directory:
`comments_data/project_<music_audit_project>/`. New layout-v2 project slugs are
semantic and unique: `music_audit_[<run-label>_]<new|refresh>_<source>_<target>_<Np|all>_<timestamp>_<microseconds>`.
The operator derives source, normalized target, cardinality, policy, and time
from the real command. `--run-label` is optional naming metadata for a purpose
the operator cannot infer, such as `test` or `brand-mie-sedaap`; it changes no
collection scope. A supplied advanced `--project` is mutually exclusive with
`--run-label`, must be lowercase, begin with `music_audit_`, omit the
`project_` folder prefix, and fit the operator's Windows-safe bound. Long
derived names are deterministically shortened with a hash rather than silently
dropping uniqueness.

Before collection, layout v2 creates one compact hash-bound `<artifact-stem>`
for every per-run file. The only canonical tree is:

```text
comments_data/project_<music_audit_project>/
  state/<artifact-stem>_state.sqlite
  <artifact-stem>_handoff.json
  <artifact-stem>_ledger.jsonl
  <artifact-stem>_evidence.jsonl    # only after validated completion
  <artifact-stem>_review.json
  <artifact-stem>_review.md
```

The handoff hash-binds the layout schema, label, descriptor, artifact stem, and
all absolute paths. A blocked run preserves the database, handoff, ledger, and
review while correctly omitting the evidence export. Existing layout-v1 runs
with `state/engage_state.sqlite` and fixed `music_audit_*` artifact names remain
canonical at their original paths and must not be renamed or migrated in
place; poll, resume, finalize, and validate must keep reading them through the
v1 compatibility path. Do not create `results.json`, `audit_results.json`, a
project CSV, or a separate export directory for a guarded MUSIC AUDIT. The
shared master registry remains outside individual run folders at
`comments_data/tiktok_master/state/tiktok_master.sqlite`.

Every LISTEN, AUDIT, or ENGAGE run must be registered in the master database at
creation and synchronized as its counters/status change. Do not delete, reset,
or replace the master database to make a post appear new. A refresh appends a
new observation and change summary; it never overwrites an earlier evidence
snapshot and never resets publication history.

For every counted post, collection must attempt and record:

- canonical TikTok post ID, URL, creator, and collection timestamp;
- caption or description;
- current public post metrics;
- platform transcript and subtitles when available, or an explicit
  unavailable/not-provided outcome;
- comments and replies that the logged-in session can legitimately access, or
  an explicit zero/unavailable outcome;
- platform-declared music metadata, or an explicit terminal not-provided,
  unavailable, or partial outcome;
- deterministic configured-catalog enrichment with a terminal outcome,
  candidate provenance, and result hash; and
- provenance plus the evidence hash used by later stages.

Canonical LISTEN music enrichment is evidence collection, not AI analysis. For
every post, the collector must attempt and hash-bind a `music_evidence` block
containing:

- TikTok declaration status bound to the enclosing evidence observation time;
- the platform-scoped music ID, declared title, declared author/artist, album
  when returned, and TikTok original-sound flag as true, false, or unknown;
- a separate `platform_contained_recording` status derived only from TikTok's
  structured `music.matched_song` / `matchedSong` declaration, falling back to
  `matched_pgc_sound` / `matchedPgcSound`; preserve its safe title, artist,
  recording ID, album, ISRC, duration, source, and field-level outcomes;
- post duration and declared music duration as separate fields;
- field-level provenance and terminal availability reasons;
- the catalog-provider set frozen when the run was created;
- each provider query shape, terminal outcome, bounded candidate set,
  deterministic match evidence, provider/result hash, and cache/circuit
  provenance;
- `acoustic_verification.status=not_attempted` with `verified=false`; and
- `lyrics.status=not_attempted`.

TikTok declaration statuses are `available`, `partial`, `not_provided`, and
`unavailable`. The initial required catalog provider is MusicBrainz. Its
terminal outcomes are `matched`, `ambiguous`, `not_found`, `unsupported`,
`unavailable`, `rate_limited`, and `provider_error`. `matched` means one
deterministically strongest catalog candidate and maps to identity status
`catalog_correlated`; it does not mean that the audible bytes or exact
recording version were verified. `not_found` is reserved for a successful query
that returned no usable candidates. `unsupported` covers a generic original-
sound label or missing required title/artist. `unavailable`, `rate_limited`,
and `provider_error` record distinct provider failures and must not be
collapsed into `not_found`.

When a contained recording supplies both title and artist, use that declaration
as the MusicBrainz query input and record
`input_basis=platform_contained_recording`; otherwise use the outer non-generic
platform sound. Safe `tt2dsp` linkage IDs alone produce a `partial` contained-
track outcome with reason `contained_recording_linkage_returned_without_identity`;
they are not by themselves a resolved identity and their tokens must never be
stored. When a validated platform `1` Apple track ID is present, the collector
must run the declared `apple_itunes_lookup` resolver against the frozen `ID`
storefront, store its closed/hash-bound scalar result separately as
`tt2dsp_resolution`, and retain the TikTok contained declaration as `partial`.
An exact Apple result may become the MusicBrainz input with
`input_basis=tt2dsp_catalog_resolution`. Apple lookup statuses are `resolved`,
`not_found`, `unsupported`, `unavailable`, `rate_limited`, and
`provider_error`; a result must bind the exact requested ID. Platform `3`
Spotify linkage may be retained for provenance but is not independently
resolved until an authorized full-metadata adapter is configured. Never retain
Apple preview/artwork URLs, Spotify embed HTML/artwork, or raw provider
payloads. A contained declaration is API-derived
TikTok metadata, not independent audio fingerprinting; acoustic verification
remains `not_attempted`.

Retain up to five MusicBrainz candidates with recording MBID, title, artist
credit and artist MBIDs, ISRCs, duration, first release date, release
identifiers/titles, provider search score, and deterministic match reasons.
Store identity as `catalog_correlated`, `platform_declared_only`, `unresolved`,
or `not_applicable`. A TikTok music ID is not a MusicBrainz ID, ISRC, or
acoustic fingerprint. TikTok's original flag and a generic `original sound`
label are platform declarations, not proof that the uploader created the
audible recording; a generic label normally produces
`unsupported` rather than a fabricated identity.
Because TikTok localizes the displayed label, the structured
`music.original`/`isOriginal=true` field is the primary language-independent
generic-original-sound gate. Localized title matching is only a fallback when
that field is unavailable. A resolved matched-song or tt2dsp catalog identity
still takes precedence over the outer original-sound wrapper.

Never persist signed audio-play URLs, cookies, media authorization values, or
transient artwork URLs. LISTEN does not download audio, run acoustic
recognition, scrape or reproduce lyrics, infer lyrical meaning, evaluate
music-to-post fit, or use Google/Spotify/Shazam/lyrics services as undeclared
providers. Do not document or enable another provider until its adapter,
provenance, terminal outcomes, usage constraints, and tests are implemented.
An identical lookup may use the collector's content-addressed in-memory cache,
but every evidence block must retain the result hash and its `cache_hit` and
`circuit_open` provenance; a cache hit must not be represented as a new
provider request. Before an actual MusicBrainz request, reserve the
workspace-global provider slot in the master registry. The current minimum
start interval is 1.05 seconds across concurrent project runs, not merely
within one collector instance. A `rate_limited` result extends that shared
cooldown by the provider's `Retry-After` value, or a conservative fallback.
Apple tt2dsp lookup uses the same workspace-global reservation mechanism under
provider `apple_itunes_lookup`, with a 3.05-second minimum start interval.

Music collection is terminal when both the TikTok declaration attempt and each
provider configured for that immutable run have a recorded terminal outcome.
This allows a counted record to preserve explicit unavailable or provider
failure outcomes instead of hanging indefinitely. Complete collection means
that every required stage was attempted and became terminal, not that TikTok
or MusicBrainz necessarily supplied every field or resolved an identity.

For a photo/carousel with no caption, record the slide count and a terminal
visual-evidence outcome. If no semantic visual description is legitimately
available, preserve that explicit unavailable outcome rather than pretending a
caption exists or leaving the record permanently partial. Such a record may be
counted, but the built-in AI must remain within the available metrics/comments
and may choose `skip`; it must not infer unseen image content.

Continue discovery, pagination, and replacement collection until `N`
evidence-ready records are stored. If exhaustion, access restrictions, or
repeated failures make `N` impossible, record `collection_incomplete` with the
verified count and reasons, report `X/N`, and stop. Do not silently accept a
smaller batch, and do not proceed to analysis, drafting, or publication.

Persist every normalized candidate as a durable, attempt-fenced checkpoint
during collection rather than buffering the entire run in memory. An explicit
`resume-collect --run-id <run-id>` continues only the saved run's immutable
source mode/target, count, account, collection policy, catalog set, and refresh
settings; it may repair incomplete evidence but may not overwrite an
evidence-ready post or exceed `N`. Partial checkpoints never unlock analysis.
If the `N`th record was committed before a
process interruption, resume may finalize `collection_complete` from those
checkpoints without touching TikTok again.

Collection completion never grants publication permission.

A `MUSIC AUDIT` shortcut creates a run with the underlying durable value
`workflow=listen`; `LISTEN` is retained as its lower-level alias. It performs
TikTok collection and deterministic configured-catalog enrichment, stores the
hash-bound complete evidence snapshot, synchronizes it to the master registry,
and stops. It cannot export or import AI analysis, draft, review, authorize, or
publish. MusicBrainz lookup does not change that boundary: it is deterministic
evidence enrichment, not built-in-AI analysis. `AUDIT` creates
`workflow=audit`, must use shadow mode, and permits only the collection and
analysis/report stages described below. `ENGAGE` creates `workflow=engage`.

A completed MUSIC AUDIT run (stored as `workflow=listen`) may use the read-only
`export-evidence --run-id <run-id> --file <path>` command. The global
`--database <path>` option must appear before the subcommand. Export is allowed
only when the run has `workflow=listen`, status `collection_complete`, no active
collection attempt, and exactly the immutable requested number of
evidence-ready rows. Before writing JSONL, the command verifies each stored
evidence JSON/hash, post-ID binding, evidence-ready flag, and applicable creator
inventory gate.

Each exported `tiktok-listen-evidence-export-v1` row carries the run, project,
source mode, collection policy, post ID, canonical raw evidence hash, compact
projection hash, and safe semantic `evidence_packet`. That packet retains the
caption, metrics, transcript/subtitle status and segments, compact comments and
replies, availability/provenance, and the complete sanitized `music_evidence`
block with TikTok declaration, MusicBrainz result/candidates, identity status,
catalog result hash, and music evidence hash. It excludes transport-only
avatars and signed media/share/audio/artwork URLs.

`export-evidence` reads stored local evidence and writes only the requested
artifact. It must never start, attach to, or revalidate Profile 7, invoke an AI
model, alter counters/status/checkpoints, or mutate project/master workflow
state. The export is not an analysis queue, LISTEN-to-AUDIT promotion,
authorization, or publication eligibility.

## Required MUSIC AUDIT BACKFILL Contract

MUSIC AUDIT BACKFILL is the reusable workspace-wide way to apply the current
music-evidence contract to older data. It operates only on posts already in the
workspace-global master registry and uses `music_backfill_tiktok.py`; it is not
`new_only`, `refresh_known`, or an in-place promotion of an older LISTEN run.

At `run` creation, accept exactly one known-post scope: normalized topic, exact
creator, one canonical TikTok URL, or one or more explicit TikTok post IDs.
After applying the target music schema and retry-status eligibility rules,
require exactly one cardinality choice: `--all-eligible` or positive
`--limit N`. Freeze the ordered post selection, canonical URL and creator,
base evidence snapshot identity and canonical evidence hash, target schema,
configured catalog providers, retry statuses, and master-database path. Resume
must reuse those values and may not discover, add, remove, reorder, or
substitute posts.

By default, a post is eligible when its latest music evidence is missing, its
schema is older than the target schema, or its terminal outcome is in the run's
frozen retry-status set. A current terminal record is skipped unless the
operator explicitly supplies `--force`; force still creates a new append-only
music observation and never rewrites old evidence. Topic and creator scopes are
selected exclusively from the registry. A creator run may inventory that exact
profile once as a music-metadata transport, but the live inventory must never
change, expand, reorder, or substitute the frozen registry selection. URL and
post-ID scopes must already exist in the registry.

For topic, URL, and explicit post-ID scopes, reuse each stored canonical URL and
attempt direct TikTok HTML music metadata first. Only when that primary attempt
fails for a frozen exact URL or post-ID target may the collector inventory the
affected exact owner once as a narrow metadata fallback. Group failed targets
by frozen owner, consume only a profile row whose post ID, owner, and video/
photo media type match the frozen binding, and ignore every unrelated profile
row. The fallback may neither expand nor substitute the frozen selection.
Topic scope is never eligible for this fallback. A terminal exact-owner
frontier that omits the failed target records that target as `unavailable`; a
nonterminal frontier cannot prove absence, so an unresolved failed target
leaves the run incomplete and resumable.

Whether direct HTML or the narrow fallback supplies it, retain only the TikTok
metadata needed for the current platform music declaration, contained-recording
fields, and safe `tt2dsp` linkage. Do not refresh caption, metrics, transcript,
subtitles, comments, replies, visual evidence, the full evidence snapshot, or
the master post's `last_seen` value, and do not acquire audio or invoke AI.
When an eligible Apple `tt2dsp` ID is available, run the configured exact-ID
Apple resolver and then MusicBrainz when the resolved or platform-declared
title/artist supports it, using the same global provider reservations,
sanitization, terminal outcomes, and uncertainty rules as canonical MUSIC
AUDIT.

Append every result as a separate hash-bound music observation with its own
observation timestamp. Bind it to the frozen post ID, base evidence snapshot
identity and hash, target music schema, provider/result hashes, and provenance.
Never mutate or re-hash the older evidence snapshot, append a new full evidence
snapshot/delta, change collection/publication history, update `last_seen`, or
make the post globally new. Deleted, private, inaccessible, or metadata-missing
posts may finish with an explicit terminal `unavailable` or other applicable
terminal result; they must not hang the whole backfill or be represented as a
catalog match.

Backfill is resumable from per-post durable checkpoints and stops when every
frozen work item has a terminal music observation. `status` and `export` are
read-only and must not attach to Profile 7, call providers, or mutate the master
database. Export contains only the selected hash-verified music observations
and their base-snapshot bindings. No backfill command may run built-in or
external AI, analyze music-to-content fit, draft or review a response, create
an approval/publication record, or publish anything.

The command surface is:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run --project $Project --creator "@maker" --all-eligible

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

For `run`, replace `--creator` with exactly one of `--topic`, `--url`, or one
or more repeatable `--post-id` values. Replace `--all-eligible` with
`--limit N` for a bounded frozen selection. Optional run controls are
`--target-schema tiktok-music-evidence-v3`, repeatable `--retry-status`,
`--force`, `--expected-account`, and `--max-pages`; `resume` accepts only the
saved `--run-id` plus operational `--expected-account` and `--max-pages`.

A LISTEN run remains collection-only and cannot be promoted or analyzed in
place. A later AUDIT must create its own canonical AUDIT or AUDIT REFRESH run.
When that run collects or refreshes the same post, its raw evidence and compact
AI projection retain the hash-bound music declaration, catalog candidates,
terminal outcomes, and provider/result hashes. AUDIT may analyze
content/music/comment relationships only from those stored fields. It must
preserve identity uncertainty, make no acoustic or lyrical claim without the
corresponding authorized evidence, and must not infer that music caused
engagement.

## Required SONIC AUDIT Contract

SONIC AUDIT is an isolated acoustic research workflow. It is not LISTEN,
MUSIC AUDIT, MUSIC AUDIT BACKFILL, AUDIT, PULSE, or ENGAGE, and it must never be
routed through those workflow values or databases. In particular, MUSIC AUDIT
continues to forbid audio download and acoustic recognition. Only
`sonic_audit_tiktok.py` may implement the permission-gated exception described
in this section.

Initial v1 ad hoc `run` accepts one source shape: exact known creator plus finite
`--posts N`, where `1 <= N <= 60`. It rejects topic, URL, explicit post-ID,
`ALL`, photo/carousel posts, live discovery, and replacement collection. The
only separate execution path is one exact validated `run-plan-batch` selection
under the contract below.
Selection reads the workspace-global master registry without writing it. At run
creation, freeze the normalized creator,
ordered unique post IDs, canonical URLs, base evidence snapshot identities and
canonical hashes, requested count, selection method, master-database path, and
the authorization assertion. If fewer
than `N` valid known exact-owner rows can be frozen, fail without lowering the
count or substituting live posts. Resume reuses the frozen manifest exactly.

`plan-corpus` is a separate query-only offline utility for constructing a
deterministic positive-pair plan before any new SONIC run is proposed. It
accepts exactly one repeatable scope: one or more `--creator` values, or one or
more exact `--post-id` values. `--exclude-run-id` is repeatable only with
creator scope; every excluded run must be complete, within that creator scope,
and bound to the same master database. The command requires
`--min-repeated-groups`, `--min-positive-pairs`, `--max-posts`,
`--max-posts-per-reference`, and `--file`. It reads the master database
query-only, performs no browser, TikTok, provider, media/audio, or AI access,
does not mutate the master database or create a SONIC run, and writes only the
requested no-clobber `tiktok-sonic-audit-corpus-plan-v1` JSON artifact. Its
eligible labels come only from hash-verified public-video rows with exact Apple
`apple_itunes_lookup` `tt2dsp` resolution in completed, current v3 music-
backfill evidence. The plan binds the exact source scope, target thresholds,
candidate/evidence hashes, excluded run IDs/hashes and post IDs, repeated
reference groups, and deterministic single-creator batches of at most 60
posts. Selection records `max-marginal-positive-pairs-v1`: after seeding the
required groups, each added post goes to the group where it creates the most
new positive pairs, with deterministic tie-breaking. Explicit post-ID scope is
exact and fails rather than dropping an
ineligible ID. A plan does not authorize or acquire audio and cannot be treated
as transient-audio permission.

`run-plan-batch` executes exactly one named batch from a stored corpus plan. It
requires `--project`, `--plan-file`, `--batch-id`, and a fresh explicit human
authorization recorded by `--authorize-transient-audio`; optional
`--expected-account` is normalized and frozen into the new run when supplied.
Planning permission, another batch's permission, or merely possessing the plan
never authorizes execution. Before any browser or media access and before run
creation, validate the plan schema/hash and source-scope/candidate/group/batch
set hashes; every individual candidate, group, and batch hash and relationship;
the selected batch ID; the frozen master-database path; and every candidate's
current master snapshot, base evidence, completed v3 music observation, exact
Apple `tt2dsp` resolution, and latest-music-observation binding. Any changed,
missing, stale, mismatched, or tampered binding fails closed.

The validated batch must contain one exact creator, 1-60 ordered public-video
candidates, and no duplicates. Freeze that order without discovery,
replacement, dropping, or addition. Preserve the typed Apple reference
(`apple_track_id`, provider, storefront, track ID, label, title/artist, and
`tt2dsp_exact_apple_id_resolution` basis) plus plan, batch, candidate, and group
hash/position provenance in the run and terminal feature records. A given plan
hash and batch ID may create only one run; a duplicate `run-plan-batch` request
must identify the existing run and be rejected, after which use `resume` on
that run. The ordinary `resume`, `status`, `validate`, `validate-suite`, and
`export` paths apply to plan-bound runs without restating the plan or batch.
Plan-bound results remain `exploratory_only`, never a production threshold or
catalog identity upgrade. It follows the same bounded transient-media,
finally-path cleanup, zero-persistent-raw-media, no-built-in-semantic-AI, and
read-only-master requirements as every other SONIC run. The separately gated
Mirelo symbolic branch below is the only optional learned-provider exception.

Transient media/audio acquisition requires contemporaneous explicit permission
from a non-AI user for each ad hoc or plan-bound run.
`--authorize-transient-audio` is an attestation that such permission was
received; it is not a permission prompt or bypass, and
the executing model must not infer or self-grant it. The authorization applies
only to the frozen run and does not carry to another creator, selection, or
batch or run. The current 60-post BankBCA pilot is therefore a fixed `N=60`
run, not an `ALL` request or permission to process the remaining registry. Its
validation-rich selection targets 21 usages from repeated exact Apple
references, 30 distinct singleton Apple references, and 9 unresolved/original
posts. When a bucket is short, fill deterministically from the remaining
eligible exact-owner pools, disclose the realized bucket counts, and never
duplicate, discover, or substitute a post. This is a stratified validation
sample, not a random sample, terminal creator inventory, or representative
full-portfolio audit; its accuracy and cluster findings apply only to the
realized labelled pilot set.

A new nonoverlapping extension run may repeat `--exclude-run-id
<prior-complete-run>` on `run`. Each excluded run must be complete and bind the
same creator and master database. Selection excludes its frozen post IDs,
prioritizes remaining externally labelled posts, freezes the exclusion hashes,
remains capped at 60 posts, and requires new transient-audio authorization.

Implementation status: the Mirelo transport adapter is not implemented in this
release. Any Mirelo-enabled run request must fail closed before run creation,
must not read a provider key, reserve credits, upload audio, or claim symbolic
results. The remaining Mirelo text is a reserved future contract rather than
current operator guidance.

Mirelo Audio-to-MIDI is an optional, declared third-party symbolic diagnostic
inside SONIC AUDIT; it is not part of MUSIC AUDIT and is not enabled by
default. A new ad hoc `run` or exact `run-plan-batch` may enable it only when
all four flags are supplied together: `--authorize-transient-audio`,
`--mirelo-audio-to-midi`, `--authorize-mirelo-upload`, and the positive
immutable run-wide ceiling `--mirelo-max-credits N`. The upload authorization
is separate from local transient-audio authorization: a non-AI user must
explicitly authorize the exact run's third-party upload and attest that they
have the rights necessary to submit that audio under the current
[Mirelo Terms](https://mirelo.ai/terms). A plan, prior run, local-audio
permission, API key, or the model's assessment of usefulness grants neither
that authorization nor those rights. The upload permission and credit ceiling
are frozen in the run manifest; `resume` reuses them unchanged and accepts no
new Mirelo flags or broader selection.

The adapter may read its bearer secret only from the process environment
variable `MIRELO_API_KEY`. Never accept the key through a CLI flag, checked-in
configuration, plan, manifest, database, prompt, log, checkpoint, error, or
export. Before each provider submission, perform Mirelo's credit/ETA preflight
for the bounded decoded-audio input and fail closed without uploading when the
sanitized estimate plus credits already committed would exceed the frozen
`--mirelo-max-credits` ceiling. Provider work is limited to the exact frozen
post being processed; it may not discover, substitute, extend, or upload any
other recording. A missing key, failed preflight, insufficient budget,
provider refusal, or ambiguous provider outcome remains an explicit bounded
symbolic outcome and must not weaken the local SONIC result.

Treat Mirelo-hosted input/output assets as potentially retained by the
provider for up to 24 hours; local cleanup cannot claim immediate remote
deletion. The user must be told this before upload authorization. Locally,
provider input audio, MIDI, MusicXML, raw/structured note events, raw
instrument tracks, provider payloads, job/result URLs, and download URLs are
transient only and must be removed or discarded through the same finally-path
cleanup. Persist only a bounded sanitized hash-bound summary: provider/model
and config provenance, terminal status, input-audio/hash binding, preflight and
credit scalars, non-reconstructable aggregate symbolic diagnostics, timings,
and result hashes. Never persist a note sequence or another representation
from which the submitted recording could reasonably be reconstructed.

Mirelo output is probabilistic supporting evidence. It must not alter the
primary local `recording_score`, similarity threshold, catalog identity, or
cluster ground truth, and it must be reported separately as a symbolic
diagnostic. It cannot support a song/artist/release identity, genre, mood,
lyrics, ownership, creator-performance, or engagement-causality claim. The
provider must not be used as an acoustic-recognition shortcut. `status` and
`export` remain offline and expose only the frozen sanitized summary; they do
not contact Mirelo, resolve result URLs, spend credits, or require the API key.
All adapter tests must use mocked preflight/upload/result transports and
synthetic fixtures only; tests must never upload live audio or consume Mirelo
credits. Implementation and operator behavior must follow the current
[Audio-to-MIDI API documentation](https://mirelo.ai/api-docs#audio-to-midi),
[model documentation](https://mirelo.ai/models/audio-to-midi), and terms.

Before any TikTok access, `run`, `run-plan-batch`, or `resume` must perform the
mandatory Profile 7 connection, profile, login, and active-handle checks. Use
only each frozen canonical URL, validate the returned post ID and exact owner,
and never discover or substitute another post. Bound each transfer and decode by explicit
timeouts, maximum bytes, maximum duration, media type, and output format. Never
print or persist cookies, request headers, signed media/audio URLs, authorization
tokens, browser payloads, or raw provider responses.

V1 processes sequentially. Its defaults are at most 64 MiB source media, 8 MiB
decoded WAV, and 180 seconds of audio per post, with a 3,600-second run budget;
absolute implementation caps are 128 MiB source, 16 MiB decoded WAV, and 300
seconds per post. HTML is capped at 8 MiB, each request at 30 seconds,
transcoding at 90 seconds, and redirects at three. Exceeding a bound is an
explicit terminal per-post or run outcome, never permission to retain a partial
file or relax cleanup.

Per-post durable terminal statuses are `completed` and `unavailable`.
Recoverable item transport failures become `unavailable` after cleanup and do
not trigger substitution; the sequential run continues. A preflight, account,
programming/callback, or run-wide budget failure leaves unprocessed items
pending and the run `sonic_incomplete` for resume. Once every frozen item has a
terminal record, including explicit unavailable outcomes, the run is
`sonic_complete`.

Every post must be processed inside a run-owned temporary location with cleanup
in a `finally` path. Raw video, extracted audio, decoded PCM, separated stems,
and intermediate spectrograms are transient and must be removed after feature
extraction on success, failure, cancellation, or resume repair. Before a run is
reported complete, verify that no raw media/audio remains in either the run
directory or its temporary workspace. A cleanup failure is a terminal error to
report and repair, not permission to retain the media silently.

The only durable executable-run output root is
`comments_data/sonic_audit_runs/<run-id>/`. It contains a versioned immutable
manifest, fenced per-post checkpoints, derived feature/fingerprint records,
and deterministic evaluation/export results. V1 uses
`tiktok-sonic-audit-run-v1`, `tiktok-sonic-audit-state-v1`,
`tiktok-sonic-audit-feature-record-v1`, and `tiktok-sonic-audit-report-v1` as
separate SONIC AUDIT schemas; verified export uses
`tiktok-sonic-audit-export-v1`. Optional offline statistical validation uses
the `tiktok-sonic-audit-statistical-validation-v1` wrapper with a nested
`sonic-statistical-validation-v1` evaluation. A multi-run suite uses
`tiktok-sonic-audit-statistical-validation-suite-v1`. The wrapped local
feature/evaluation schemas are `sonic-feature-v1`,
`sonic-similarity-report-v1`, and `sonic-cluster-stability-v1`. Durable records
may contain the
frozen master bindings, decoded-audio content hash, safe scalar quality and
duration fields, local fingerprints, numeric embeddings/features, similarity
scores, cluster IDs, thresholds, software/model/version provenance, timings,
terminal outcomes, and result hashes. They must not contain raw or encoded
media/audio, reconstructable waveforms, signed transport URLs, secrets, comment
or transcript refreshes, or a copied master database. SONIC AUDIT never writes
the master registry, updates `last_seen`, appends evidence/music observations,
changes global-new or publication history, or creates a canonical project run.

The offline, non-run `plan-corpus` command is the sole exception to that run
directory rule. It creates only the operator-named no-clobber plan JSON;
normally place it under `comments_data/sonic_audit_plans/`. A plan directory is
not a run directory and may not contain media, audio, feature checkpoints, or
authorization state.

Fingerprint and embedding comparisons support only measured similarity within
the frozen set. They are not Shazam-style catalog identification and cannot by
themselves name a song, artist, release, ISRC, MusicBrainz recording, or Apple/
Spotify item. A catalog identity may be displayed only when corroborated by an
independently stored, hash-bound TikTok/Apple/MusicBrainz declaration associated
with the frozen base evidence, with that provenance and uncertainty preserved.
Acoustic similarity must not upgrade or overwrite the catalog evidence. A
generic original-sound wrapper remains unproven, and unresolved is a valid
terminal result.

Feature extraction, fingerprint matching, clustering, and primary evaluation
are local deterministic computation. Do not invoke built-in or external
semantic AI once per post, generate semantic labels from sound without ground
truth, infer lyrics, score a person or content portfolio, claim that audio
caused engagement, draft/review a response, create approval/publication state,
or publish. The optional Mirelo path above is the sole declared v1 learned-
model exception: it performs separately authorized symbolic transcription,
has frozen provider/model/config and usage constraints, and remains a separate
diagnostic that cannot change the local baseline.
The v1 baseline uses local deterministic DSP fingerprints and numeric feature
vectors; it does not download or invoke Essentia, CLAP, Shazam, AcoustID lookup,
or another pretrained/external recognition service. Mirelo is not a catalog
lookup or acoustic-identity provider.

For the 60-post pilot, report at minimum: selected/processed/terminal coverage;
completed, unavailable, and pending counts plus any run error; total and
per-post acquisition, decode, feature, and comparison timing; persistent raw-media count after
cleanup; similarity/cluster coverage and abstention; and feature/version hashes.
Use the transport's monotonic per-item fields `html_fetch_ms`,
`media_download_ms`, `inspect_transcode_ms`, `processor_ms`, and
`total_item_ms`, plus run fields `preflight_ms`, `attach_ms`, `transport_ms`, and
`total_run_ms`; do not estimate missing timings.
When independent same-recording labels exist, use post-disjoint evaluation and
report labelled support, evaluable/resolved/unresolved query counts,
abstention rate, Recall@1, Recall@5, precision at the chosen threshold,
true-positive rate, and false-match rate. Report cluster stability as
mean/minimum/maximum adjusted Rand index with its window-bootstrap or aggregate-
vector resampling basis, separately from identity accuracy. If the frozen set
lacks enough independent positive and negative labels, mark an accuracy metric
`not_evaluable`; never turn cluster cohesion or a catalog-correlated title into
fabricated ground truth.

The v1 command surface is:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  --output-root $SonicRoot `
  plan-corpus --creator $Creator `
  --exclude-run-id $PriorCompleteRun `
  --min-repeated-groups $MinRepeatedGroups `
  --min-positive-pairs $MinPositivePairs `
  --max-posts $MaxPosts `
  --max-posts-per-reference $MaxPostsPerReference `
  --file $CorpusPlan

& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  --output-root $SonicRoot `
  run-plan-batch --project $Project `
  --plan-file $CorpusPlan --batch-id $BatchId `
  --authorize-transient-audio `
  --expected-account $ExpectedAccount

& $EngagePython .\sonic_audit_tiktok.py `
  --master-database $MasterDatabase `
  run --project $Project --creator "@bankbca" --posts 60 `
  --authorize-transient-audio

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

`validate` is an offline post-run stage and is valid only for a completed,
immutable SONIC AUDIT run. It verifies and binds the manifest hash, source
report hash, and hash of the ordered feature-record set, then writes only
`comments_data/sonic_audit_runs/<run-id>/statistical_validation.json`. It must
not attach to Profile 7, access media/audio, invoke AI, write the master
registry, rewrite `report.json` or `state.json`, or require a new transient-
audio authorization. Its reference-label-disjoint out-of-fold calibration is
reported separately from full-sample descriptive positive/negative score
distributions and the complete threshold sweep. It also reports a reference-
grouped bootstrap and finite-support Wilson intervals explicitly labelled as
diagnostics. With the current BankBCA support of eight repeated reference IDs
and ten positive pairs, `recommendation_status` must remain
`exploratory_only`; validation is not a production threshold approval.

`validate-suite` is offline-only. It requires at least two ordered unique,
complete, nonoverlapping runs with the same creator, master binding, and feature
contract. It binds all source hashes and the combined feature-set hash, then
creates the requested `tiktok-sonic-audit-statistical-validation-suite-v1`
file without clobbering an existing target. It performs no browser,
media/audio, master-registry, or AI access. The combined result remains
`exploratory_only`.

`status` and `export` remain local read-only commands: they must not attach to
Profile 7, acquire media, recompute features, invoke AI, or mutate the master or
run. Export verifies the frozen bindings and stored hashes and includes the
statistical-validation artifact when it is present. The authorization flag is
accepted only at new run creation; resume must use the frozen authorization and
may not change the creator, count, selection, base bindings, or authorization.
Each completed feature record separately stores and hash-binds its effective
feature schema,
algorithm, and configuration; transport receipts bind fixed v1 conversion
provenance and timings. Resume is permitted only under the unchanged deployed
feature/transport implementation. After a code, dependency, algorithm, or
configuration change, do not mix new records into the older run.
`run`, `run-plan-batch`, and `resume` may accept the optional operational
`--expected-account`; that active Profile 7 account is independent of the
creator being audited and must match exactly when supplied. New `run` and
`run-plan-batch` creation may additionally accept only the complete Mirelo
flag gate defined above. `resume` accepts no Mirelo flags and must honor the
frozen provider/upload/budget configuration. For a plan-bound run, a supplied
expected account is frozen and resume must honor it. Omit the global
`--output-root` in production so the required isolated default root is used.

## Required PULSE Sequence

Every PULSE request must use this separate stopping path:

```text
Profile_7_browser_and_account_preflight
-> one_bounded_topic_or_creator_pass_or_one_direct_URL
-> deduplicate_within_the_sample
-> shallow_in_memory_projection
-> one_compact_built_in_AI_analysis_batch
-> deterministic_noncanonical_quick_signals
-> display
-> discard
-> stop
```

Use only `quick_audit_tiktok.py`. Its `collect` command emits a compact JSON
packet to standard output. The current interactive Codex/Antigravity model must
analyze every sampled row in one batch without an external LLM API, then pass
`{snapshot, analyses}` through the `report --actor <built-in-AI-identity>`
validator on standard input. The report command is local and must not touch
Profile 7.

PULSE's positive `N` is a sample target, not an exact evidence-ready contract.
It makes one search page or one finite profile page and may return `X/N` with
the reason. It does not expand related queries, hydrate replacements, enumerate
a terminal creator frontier, consult the global-new registry, or fail the whole
sample merely because `X < N`. `ALL`, refresh, resume, and promotion into a
canonical run are invalid. A fresh LISTEN, AUDIT, or ENGAGE collection is
required for canonical use.

The shallow packet may retain only the canonical post ID/URL and creator,
observation time, caption or description when directly returned, current
public metrics, and an explicit directly available visual description. Deep
audiovisual interpretation, transcript/subtitle retrieval, comment text,
replies, and unseen photo/carousel semantics are omitted for speed. A row with
no caption or direct visual description remains `unrated`; metrics alone must
not be used to guess content quality.

PULSE creates no project or master database row, run ID, checkpoint, queue
file, evidence/report hash, stored report, response type, draft, review,
presentation, approval token, authorization, handoff, publication row, or
receipt. Required output flags are `non_canonical=true`, `ephemeral=true`,
`persisted=false`, `not_person_rating=true`, and
`publication_eligible=false`. Every result must show requested, sampled,
analyzed, rated, and unrated counts; sampling method; per-post links; present
and omitted fields; signal denominators; confidence; limitations; and this
banner:

```text
NON-CANONICAL, EPHEMERAL SNAPSHOT — sample-based; not a full AUDIT, not a creator/person rating, not publication eligibility, and not comparable across runs.
```

For each rateable row, the AI supplies `post_quality_score` and the shallow
interest proxy `conversation_value_score` on 0-100 scales. Code reuses the
bounded 80/20 arithmetic only to calculate `quick_overall_score`; it must not
call that result canonical `analysis_score`, `conversation_value`,
`engage_suitability`, or a public-comment rating. Aggregate equal-weight means
are displayed as `quick_content_signal`, `quick_interest_signal`, and
`quick_overall_signal` on `/10`, with decimal round-half-up to one decimal.
Zero rateable rows produce unavailable signals rather than `0.0`.

## Required AUDIT Sequence

Every AUDIT run must move through this exact order:

```text
browser_preflight
-> collect_or_refresh
-> verify_exact_count_and_evidence
-> analyze_every_post_with_built_in_AI
-> deterministically_aggregate_the_complete_analysis_set
-> calculate_provisional_portfolio_ratings
-> store_hash_bound_tiktok-audit-report-v1
-> audit_complete
-> stop
```

The final valid analysis import automatically calculates and stores the report.
`audit-report --run-id <run-id>` verifies and displays that stored report; it is
read-only and cannot advance workflow state. The report uses
`creator-portfolio-v1` for a creator source and `topic-portfolio-v1` for a topic
source. It binds the immutable run scope, complete analysis set, rubric,
`analysis_set_hash`, report body, and `report_hash`. Required top-level report
identity includes `run_id`, `schema_version`, `rubric_version`,
`analysis_set_hash`, `source_mode`, `requested_count`, and `analyzed_count`.
Its coverage must identify the requested or frozen inventory count,
evidence-ready count, analyzed count, response-type distribution, evidence
completeness, observation window, and any source limitations. Analysis response
classifications are measurement metadata only in AUDIT; they do not create
reply work.

AUDIT is a hard stopping boundary. It may use `export-analysis` and
`import-analysis`, but it must reject response reclassification, draft export
or import, AI response review, response storage, `show-response`,
authorization, handoff, comment or showcase publication, and all publication
commands. It creates no response, presentation, approval token, authorization,
publication row, or receipt. Deterministic formula/hash validation of the
report is not an independent AI response review.

### Provisional AUDIT ratings

Calculate each rating from all analyzed posts in that one immutable audit set,
with equal weight per post and decimal round-half-up to one decimal place:

```text
content_quality = round_half_up(mean(post_quality_score) / 10, 1)
engage_suitability = round_half_up(mean(analysis_score) / 10, 1)
conversation_value = round_half_up(mean(conversation_value_score) / 10, 1)
rateable_content_quality =
    round_half_up(mean(post_quality_score where response_type != skip) / 10, 1)
evidence_completeness_percent =
    round_half_up(mean(data_completeness), 1)
```

Always show the numerator/denominator for `rateable_content_quality`, its
coverage over all analyzed posts, the complete response-type distribution, and
the supporting `evidence_completeness_percent` diagnostic.
If no post qualifies for a denominator, store the affected rating as
unavailable rather than `0.0`. A verified empty `ALL` inventory may complete as
`0/0`, but every rating is unavailable. An incomplete fixed-count collection
cannot analyze or produce an audit report.

The report must set `provisional_internal=true` and `not_person_rating=true`.
`content_quality` summarizes the analyzed content portfolio, not the creator as
a person;
`engage_suitability` summarizes the canonical evidence-grounded analysis
scores, not permission to comment, an expected publication rate, or a public
comment rating. `conversation_value` is a supporting portfolio signal.
`rateable_content_quality` excludes `skip` rows and is therefore
selection-biased; any positive-support-only average is more strongly
selection-biased, must be marked as such, and must never be substituted for the
overall `content_quality`. Reports must retain their exact scope, counts,
dates, freshness, rubric version, and hashes and must not silently merge
incompatible runs or rubric versions.

## Required ENGAGE Sequence

Every post must move through the following order without skipped stages:

```text
browser_preflight
-> collect
-> verify_exact_count_and_evidence
-> analyze_with_built_in_AI
-> classify_response_type
   (positive_support | constructive_suggestion |
    constructive_correction | clarifying_question | skip)
-> draft_with_built_in_AI
-> independent_built_in_AI_review
-> store_reviewed_final_response
-> show_exact_response_to_named_human_and_issue_bound_one_time_token
-> explicit_user_authorization
-> publication_preflight_and_revalidation
-> publish
-> store_receipt
```

The built-in AI is the interactive Codex/Antigravity assistant in the current
task, not an external LLM API and not a placeholder-generating script.
Analysis, drafting, and review must use the stored evidence packet for that
specific post.

AI queue exports may use a compact semantic projection of that packet to remove
signed media URLs, avatars, share payloads, and other transport-only fields.
The projection must retain the caption, metrics, transcript/subtitle results,
all collected comment and reply text plus useful thread signals, sanitized
platform music declarations, catalog candidates and terminal outcomes,
provider/result hashes, availability, provenance, and observation time. It
must omit signed audio and artwork transport URLs, embed the canonical raw
evidence hash, and carry its own projection hash. Raw evidence remains
unchanged in SQLite and continues to govern every import, review, freshness,
and publication check.

Downstream AI must distinguish `platform_declared_only`,
`catalog_correlated`, and acoustically verified identity. Catalog correlation
alone is never acoustic verification. No lyrical-fit claim is permitted when
authorized lyrics evidence is absent, and engagement metrics must not be used
to claim that music caused performance.

For a large run, partition independent post records into bounded AI work
batches and process those batches in parallel when agent capacity is available.
Keep one canonical database/import path, never split one post's evidence across
workers, and ensure the critic for a response is independent from its drafter.
Parallelism may reduce latency but may not weaken exact-count, hash, ordering,
or review gates.

The review is a distinct critic pass after drafting. It checks grounding,
usefulness, tone, language, unsupported claims, AI disclosure, response-type
consistency, and the applicable rating policy. Positive-support responses
must contain their canonical rating. Constructive suggestions, corrections,
and clarifying questions must contain no rating. Corrections require direct
stored-evidence support; questions must not turn uncertainty into an asserted
fact. Prefer a separate AI context or sub-agent for this pass. A failed review
returns the response to drafting; it does not authorize publication.

Store the exact reviewed response, its hashes, scores, evidence version, and
review result in the database before asking for live authorization. AI review
approval means the content passed review; it is not user authorization to
publish.

After storage and before authorization, run `show-response` only while its
target URL, response type, applicable rating state, and exact final rendered
response are actually shown to the named non-AI human. Store the resulting
presentation hash. The command issues a one-time approval token bound to that
named human, target, exact response, draft hash, and review hash. Re-running
`show-response` invalidates the earlier token. `show-response` records
presentation only; it never authorizes, hands off, or publishes.

The same named human must then explicitly approve the exact shown response.
Authorization must supply the matching presentation hash and one-time approval
token, and the authorizer identity must match the presentation identity. An
`ENGAGE LIVE` request that merely identifies a stored response/publication ID
starts this presentation process; it does not approve unseen text. A collection
request, analysis request, earlier statement of live intent, presentation
record by itself, or database row whose mode happens to be `live` is not
sufficient authorization.

## Response Types, Scoring, and Published Rating

Keep `post_quality_score` and `conversation_value_score` as separate internal
0-100 values. Code combines them under the configured bounded weighting policy
into the canonical 0-100 `analysis_score`; conversation value cannot make a
post publishable when its post-quality score is below the positive threshold.

Every analysis must select exactly one `response_type`:

- `positive_support`: a positive, evidence-grounded insight. It must pass the
  existing post-quality, overall-score, conversation, confidence, and
  completeness gates.
- `constructive_suggestion`: one practical, respectful improvement.
- `constructive_correction`: one material correction directly supported by
  stored evidence, with high grounding confidence.
- `clarifying_question`: a good-faith question used when the evidence supports
  a useful uncertainty but not a declarative correction.
- `skip`: no sufficiently safe, grounded, or useful comment opportunity.

Constructive types use separate internal `response_opportunity_score` and
`grounding_confidence` gates. Low source quality does not by itself prevent a
constructive response, but no type may bypass evidence completeness,
grounding, safety, independent review, presentation, or authorization.
`blocking_risk_flags` records unresolved risks in publishing the proposed
response itself; source-post weaknesses that the response safely addresses
belong in its rationale and evidence references, not in that blocker list.

For an eligible `positive_support` comment, derive the public rating from the
canonical `analysis_score` by converting it to `/10` and rounding to the
nearest tenth (0.1 increment):

```text
public_rating = round_half_up(analysis_score / 10, 1)
```

Examples: `82 -> 8.2/10`, `83 -> 8.3/10`, `84 -> 8.4/10`, and
`100 -> 10/10`.

Render the canonical rating into a positive-support response before AI review
and storage. Constructive suggestions, constructive corrections, and
clarifying questions must contain no `{PUBLIC_RATING}` placeholder, numeric
`/10` rating, or enabled public-rating metadata. All response types retain an
explicit AI disclosure.

Store the response type, internal scores, conditional rating metadata, exact
final text, and rendered-text hash. The publication adapter must submit and
verify that exact text. It must reject a missing or mismatched rating for
`positive_support`, and reject any rating for a constructive response.

`skipped` now means that no safe, grounded, and useful response can be
produced—not merely that the post is unsuitable for positive endorsement.

## Collected Count Is Not Published Count

`N` is the evidence-ready collection target, not a publication quota. After
analysis and review, any number from zero through `N` may be eligible for a
comment. Never lower quality, evidence, freshness, or safety thresholds merely
to publish `N` comments.

Do not impose an arbitrary low local publication quota on responses that have
independently passed every gate. If all `N` posts produce safe, grounded,
reviewed responses and the named human explicitly authorizes every exact
response, the tooling must be capable of publishing all `N`. A batch command
may process that authorized set, but it must still claim, revalidate, submit,
and store a receipt for one identified response at a time. Any operator-chosen
cap or cadence must be explicit and configurable rather than a hidden default.
This rule does not turn `N` into a publication quota: `skip`, review failure,
stale evidence, missing authorization, platform refusal, or another failed
guard still prevents the affected response from publishing.

Record at least these run counters:

- `requested`
- `unique_collected`
- `evidence_ready`
- `analyzed`
- `drafted`
- `reviewed`
- `stored`
- `authorized`
- `published`
- `skipped`
- `failed`

Collection completion is governed by `status=collection_complete` and
`evidence_ready=requested`. `unique_collected` counts every durable unique
run-post row, including candidates that failed evidence readiness and were
replaced; `failed` likewise may remain nonzero. A completed topic or creator
run may therefore have `unique_collected > requested` and `failed > 0`. Report
all counters without rewriting them. This never authorizes substitution for an
exact direct-URL run.

## Publication Guards

Immediately before submitting one specific response, verify:

- the designated browser connection, TikTok login, and expected account;
- the post is still reachable and the evidence is fresh;
- the evidence hash, analysis input hash, final text hash, and score agree;
- the response passed independent AI review, was shown exactly to the named
  human, and has presentation-token-bound explicit authorization from that
  same human;
- duplicate-comment protection and any explicitly configured rate/daily
  limits pass; and
- the exact final rendered response contains its AI disclosure and obeys its
  type-specific rating policy: exactly one canonical `/10` rating for
  `positive_support`, and no rating for a constructive response.

Publish one identified response at a time and store a receipt containing the
target, account, exact submitted text/hash, timestamp, and observed result.
When several responses are authorized, process them sequentially under this
same per-response rule; do not discard eligible feedback merely because a
batch contains more than five responses.
The local publisher default is count-agnostic and unlimited; a positive cap or
cadence is used only when the operator supplies it explicitly. A batch stops on
the first failed, uncertain, or otherwise unverified outcome and leaves later
approved responses untouched unless the operator explicitly requests
continue-on-error behavior.
Failures must be recorded without marking the response published.

## TikTok Comment Showcase

After every comment has a confirmed publication receipt, the publisher must
attempt the separate TikTok-only `COMMENT SHOWCASE` capture/enqueue step. This
creates a TikTok photo post from an exact screenshot of the published comment.
It is not TikTok's unrelated Repost feature, and the authorization that
published the comment does not authorize this second post.

The required sequence is:

```text
confirmed_comment_receipt
-> uniquely_locate_the_exact_published_comment
-> capture_and_hash_the_comment_element
-> normalize_to_an_API_ready_JPEG
-> bind_the_exact_public_HTTPS_media_URL_and_hash
-> draft_caption_with_built_in_AI_from_stored_source_evidence
-> independent_built_in_AI_caption_review
-> store_reviewed_media_caption_and_hashes
-> show_exact_image_caption_source_link_account_post_settings_and_music_usage_consent_to_named_human
-> separate_presentation_bound_human_authorization
-> Profile_7_and_exact_TikTok_account_preflight
-> verify_hosted_media_bytes_and_workspace_global_duplicate_fence
-> official_TikTok_Content_Posting_API_submit
-> bounded_status_reconciliation
-> store_local_and_master_receipts
```

The screenshot is an auxiliary action after the original comment receipt is
durable. Screenshot, media-hosting, caption, or showcase failures must never
change a confirmed comment to failed or cause that comment to be retried.
Use `recover_tiktok_comment_capture.py` for a missing screenshot; it may only
reopen the confirmed source post, uniquely recapture the exact comment, attach
the proof, and enqueue the showcase. It must never enter or submit comment
text.

Deterministic JPEG preparation is automatically discovered from the
secret-free `showcase_auto_prepare.json` beside the scripts, when that file is
present. It must remain disabled until its local paths, verified HTTPS media
prefix, and FFmpeg executable are configured and `public_media_verified` is
explicitly true. An alternate config may be supplied with
`--showcase-auto-prepare-config`. Preparation stops at
`publish_media_ready`: models must still perform built-in-AI caption drafting,
independent review, exact presentation, and separate human authorization.

`NO-API` applies to AI analysis, response drafting, caption drafting, and AI
review: those stages use the interactive built-in Codex/Antigravity model, not
an external LLM API. It does not prohibit verified TikTok data requests or the
official TikTok Content Posting API from transporting an already reviewed and
separately authorized final showcase post.

The caption must be grounded in the stored reference-post evidence and exact
published comment, disclose AI assistance, identify the source creator, and
contain the canonical source-post URL exactly once. Treat that URL as caption
text whose clickability is unverified; never promise that TikTok will render
an arbitrary caption URL as clickable.

The exact presentation must also show TikTok's Music Usage Confirmation
consent declaration together with the selected privacy, comment, music, and
commercial-content settings. The named human's separate approval is bound to
that declaration and those exact settings; it cannot be inferred from the
original comment authorization.

The publish image must be the exact reviewed API-ready JPEG. TikTok
`PULL_FROM_URL` media must use a stable public HTTPS URL on the developer's
verified domain or URL prefix, return the same approved bytes without a
redirect, and satisfy the current photo limits. Access tokens remain in memory
only and must never be printed or stored in SQLite.

Before live API submission, revalidate Profile 7 and its active TikTok handle,
then require the API `creator_info` username to match that same account.
Require the explicitly authorized privacy level; never silently downgrade a
public post to private. A confirmed result requires TikTok
`PUBLISH_COMPLETE`, exactly one public post ID, and stored verification of the
matching account/remote result. A missing ID, timeout, or failure after durable
submit intent is `uncertain` and must be reconciled before any retry.

Only `publish_comment_showcase.py` may drive claim, submit-intent, outcome, and
reconciliation transitions because it attaches the workspace-global master
fence. The local state CLI must not expose raw versions of those transitions.
A stranded `reserved` attempt may be explicitly released only when no durable
submit intent exists. A `submit_intent` or `uncertain` attempt must query and
verify the existing TikTok publish; reconciliation must never initialize a new
post.

The workspace-global master database fences one showcase per confirmed source
comment and includes confirmed showcase post IDs in the known-post set. This
prevents another project from showcasing the same comment or later collecting
and commenting on the workspace's own showcase post. Local and master
reservation, submit-intent, and outcome transitions must commit atomically.

## Prohibited Active-Workflow Paths

- Never use `incremental_project.py` or `run_scraper.py` for LISTEN, AUDIT,
  ENGAGE, PULSE, or SONIC AUDIT. They are bulk LISTEN-era orchestrators and can
  trigger campaign-wide sweeps.
- Never add transient media acquisition to MUSIC AUDIT, MUSIC AUDIT BACKFILL,
  AUDIT, ENGAGE, or PULSE. Only the separately authorized
  `sonic_audit_tiktok.py` runner may use the bounded transient-media exception,
  and its derived results must remain outside canonical workflow state.
- Never run `legacy/one_off_state_mutators/enrich_music_data.py` against an exported canonical AI queue.
  Canonical music enrichment must occur before evidence hashing and checkpoint
  storage through the collector-owned enrichment stage.
- Never use a full `refresh_known` run merely to upgrade old music fields when
  MUSIC AUDIT BACKFILL is requested. Use only `music_backfill_tiktok.py` and do
  not refresh or overwrite captions, metrics, transcripts, comments, replies,
  evidence snapshots, or master-registry freshness.
- Use only verified targeted TikTok discovery/collection paths. A single-post
  fetch must accept a specific TikTok URL or video ID. Canonical LISTEN, AUDIT,
  and ENGAGE batch collectors must enforce the exact-count contract above;
  PULSE alone uses its documented one-pass `X/N` sampling contract.
- Never use direct SQL injection, fabricated analysis rows, manual approval
  scripts, empty-packet hashes, or hash-repair scripts to bypass workflow
  state.
- Never advance directly from collected data to an approved or published
  comment.
- Never claim `show-response` was completed unless the exact printed response
  was shown to the named human.
  Never fabricate, reuse, or transfer an approval token to a different identity, target, response, or review.
- Never infer live authorization from scraping, testing, analysis, drafting,
  AI review, `show-response`, `NO-API`, or `SHADOW`. Live authorization must be explicit.
- Never write a PULSE packet or result into canonical workflow state, use it to
  draft a response, or treat its quick signals as authorization or publication
  eligibility.

See `WORKFLOWS.md` for the operational sequence and examples.
