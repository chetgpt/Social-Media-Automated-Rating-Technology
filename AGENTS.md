# TikTok ENGAGE Workspace Contract

This workspace is operated as a TikTok-only evidence and engagement project
with three active workflow modes: `LISTEN` collects evidence, `AUDIT` collects
and analyzes evidence, and `ENGAGE` continues through response work. Older
multiplatform collection and reporting code may remain in the repository for
reference, but it is not a default workflow and must not be used to run a
LISTEN, AUDIT, or ENGAGE request.

Treat the shortcuts and gates below as persistent instructions in every task
opened from this workspace.

## Chat Shortcuts

- `LISTEN: <topic>, <post amount>` or `gather data for <post amount> posts`
  means the collection-only phase of TikTok ENGAGE. It stops after verified
  evidence has been stored. It never analyzes, drafts, approves, or publishes.
- `LISTEN CREATOR: <@handle or profile URL>, <post amount or ALL>` uses the
  exact creator profile as the discovery source. It inventories that creator's
  public TikTok posts, stores evidence for the selected new posts, and stops
  before analysis.
- `LISTEN REFRESH: <topic>, <post amount>` means collection-only incremental
  refresh of known posts. Use `refresh_known`, append new evidence snapshots
  and deltas, and stop before analysis. Explicit post IDs may be supplied
  instead of a topic selection.
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

On this workstation, run ENGAGE scripts with
`C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe`. Do not use
bare `python` from the workspace and do not use `py -3`; the launcher is not
registered for this installation. Models must resolve and invoke the real
interpreter before browser preflight instead of treating an interpreter error
as a browser failure.

## Mandatory Social-Browser Preflight

The only valid social browser is Microsoft Edge's existing user-data root with
profile directory `Profile 7`, attached in `existing_profile_attach` mode. Its
visible Edge label may be `Profile 1`; the directory `Profile 7` is the
canonical identity. This profile intentionally contains logged-in sessions for
multiple social networks. That is expected; TikTok ENGAGE uses only its TikTok
session and verifies the active TikTok handle.

Before TikTok discovery, live collection or evidence refresh, or any outbound
TikTok action:

1. The executing model must start or reuse Profile 7 itself. The
   `engage_tiktok.py collect` command performs this automatically before
   collection. For diagnosis, the equivalent low-level commands are
   `<required-python> social_browser.py start` and
   `<required-python> social_browser.py status`.
2. Verify that the local debugging connection is reachable, the connected
   profile is exactly Profile 7, TikTok is logged in, and the active TikTok
   handle can be resolved. When an expected account is specified, require an
   exact match; otherwise bind the resolved handle to the run.
3. Keep that shared Profile 7 session running for the workflow. Workers may
   open and close temporary tabs but never close the browser.

Built-in AI analysis, AUDIT report generation, drafting, independent review,
response storage, `show-response`, and authorization operate only on stored
evidence and local workflow state. They must not start, attach to, or
revalidate Profile 7. Revalidate only when TikTok is actually accessed: during
collection or an evidence refresh, and immediately before publication.

Never ask the user merely to start Edge: first attempt the automatic Profile 7
startup/reuse path. Never substitute Edge Default, a managed or temporary
profile, Playwright's new profile, or the in-app browser. If startup fails,
diagnose or report the launcher failure without falling back. Ask the user to
interact with the browser only after Profile 7 is verified running and its
TikTok session is actually logged out, its account navigation cannot be
resolved after retry, or its active TikTok handle differs from the expected
handle. Any login or account switch must happen inside that same Profile 7
window.

Repeat the connection, login, and exact account checks immediately before
publication. Never print or persist cookie values, authorization headers, or
session tokens; ENGAGE browser credentials remain in memory only.

## Exact Collection Contract

A request for `N` posts succeeds only when exactly `N` unique TikTok post IDs
have evidence-ready records. Search results, duplicate URLs, inaccessible
posts, and partial records do not count toward `N`.

`N` is any positive requested count; `50` is an example, not a coded maximum.
Discovery, storage, AI queues, authorization, and sequential publication must
derive their bounds from the requested run and must not contain a hidden
50-post ceiling. For a very large request, genuine source exhaustion,
platform refusal, or an explicit operator-supplied resource bound may still
produce `collection_incomplete: X/N`, but the workflow must never silently
truncate the request to 50.

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

For fixed creator cardinality, exactly `N` globally-new evidence-ready posts
must still be checkpointed. For `ALL`, first freeze the terminal inventory,
remove globally known IDs under `new_only`, store the selected ordered ID set
and inventory hash, then derive the run's requested count from that frozen set.
An empty verified profile may therefore complete as `0/0`; an unresolved
profile may not. Resume reuses the frozen inventory and cannot add, remove,
reorder, or substitute posts after terminal freeze.

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
selection, staleness cutoff, and master-database path.

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
  an explicit zero/unavailable outcome; and
- provenance plus the evidence hash used by later stages.

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
topic, count, account, and collection settings; it may repair incomplete
evidence but may not overwrite an evidence-ready post or exceed `N`. Partial
checkpoints never unlock analysis. If the `N`th record was committed before a
process interruption, resume may finalize `collection_complete` from those
checkpoints without touching TikTok again.

Collection completion never grants publication permission.

A `LISTEN` shortcut creates a run with `workflow=listen`. It stops after exact
collection and master-registry synchronization; an analysis export from that
run must be rejected. `AUDIT` creates `workflow=audit`, must use shadow mode,
and permits only the collection and analysis/report stages described below.
`ENGAGE` creates `workflow=engage`.

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
all collected comment and reply text plus useful thread signals, availability,
provenance, and observation time. It must embed the canonical raw evidence hash
and carry its own projection hash. Raw evidence remains unchanged in SQLite and
continues to govern every import, review, freshness, and publication check.

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

- Never use `incremental_project.py` or `run_scraper.py` for LISTEN, AUDIT, or
  ENGAGE. They are bulk LISTEN-era orchestrators and can trigger campaign-wide
  sweeps.
- Use only verified targeted TikTok discovery/collection paths. A single-post
  fetch must accept a specific TikTok URL or video ID; a batch collector must
  enforce the exact-count contract above.
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

See `WORKFLOWS.md` for the operational sequence and examples.
