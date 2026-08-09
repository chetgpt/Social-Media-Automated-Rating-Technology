# TikTok LISTEN, AUDIT, and ENGAGE Workflows

`AGENTS.md` defines the binding workspace contract. This guide shows how to
operate its three active modes: LISTEN collects evidence, AUDIT collects and
analyzes evidence into a provisional portfolio report, and ENGAGE continues
through response work. TikTok is the only active platform in this workspace.

On this workstation, first set
`$EngagePython = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"`.
Use `& $EngagePython` wherever an older example says `py -3`; do not invoke a
bare workspace-relative `python`.

## Start With the Social Browser

Every collection command automatically starts or reuses Microsoft Edge's real
`Profile 7` before discovery. The model must perform this startup itself; the
user is not expected to launch Edge first. Profile 7 may display the label
`Profile 1`, and it intentionally contains sessions for multiple social
networks. The active TikTok workflows verify and use only the TikTok session.

The launcher performs a bounded automatic retry before declaring Profile 7
blocked. A transient live-debugging enable failure must therefore be retried
inside the same run rather than creating an avoidable second run.

The automatic operation is equivalent to these diagnostic commands:

```powershell
& $EngagePython social_browser.py start
& $EngagePython social_browser.py status
```

The launcher accepts only Edge's default user-data root, directory `Profile 7`,
in `existing_profile_attach` mode. It fails closed rather than creating Edge
Default, a managed/temporary profile, or an in-app browser session.

Do not begin TikTok discovery or collection until the automatic preflight
confirms Profile 7 is reachable, its TikTok session is logged in, and the
active handle resolves. If an expected publishing account is configured,
require an exact match; otherwise bind the resolved handle to the run. Leave
Profile 7 running and repeat the same checks immediately before publication.

Ask the user to sign in, load TikTok home, or switch accounts only after the
automatic Profile 7 startup succeeds and that specific human action is still
required. A launcher/controller error must be diagnosed or reported; it is not
permission to use another browser profile.

## Shortcuts

```text
LISTEN: 3D printing, 50 posts
LISTEN REFRESH: 3D printing, 50 posts
LISTEN CREATOR: @maker, ALL
AUDIT: 3D printing, 50 posts
AUDIT REFRESH: 3D printing, 50 posts
AUDIT CREATOR: https://www.tiktok.com/@maker, ALL
AUDIT CREATOR REFRESH: @maker, 25 posts
ENGAGE: Indonesian photography, 10 posts
ENGAGE REFRESH: Indonesian photography, 10 posts
ENGAGE CREATOR: https://www.tiktok.com/@maker, ALL
ENGAGE CREATOR REFRESH: @maker, 25 posts
ENGAGE SHADOW: Indonesian photography, 10 posts
ENGAGE NO-API: cybersecurity education, 25 posts
ENGAGE LIVE: publish reviewed response <publication-id>
```

- `LISTEN` is collection-only for TikTok ENGAGE and stops after evidence
  verification and storage.
- `LISTEN REFRESH` refreshes known evidence, appends snapshots/deltas, and
  remains collection-only.
- `LISTEN CREATOR` inventories one exact creator and stops after storing the
  requested or complete globally-new evidence set.
- `AUDIT` collects exactly the requested topic evidence, analyzes every post
  with the built-in AI, stores a provisional portfolio report, and stops.
- `AUDIT REFRESH` refreshes a fixed immutable set of known topic posts,
  analyzes those new observations, stores their report, and stops.
- `AUDIT CREATOR` uses one exact creator profile and supports a fixed new-post
  count or `ALL` globally-new posts in the verified terminal inventory.
- `AUDIT CREATOR REFRESH` refreshes and analyzes a fixed number of known posts
  owned by that exact creator. It does not alter comment-publication history.
- `ENGAGE` defaults to `ENGAGE SHADOW`.
- `ENGAGE REFRESH` analyzes newly refreshed evidence but retains the global
  same-account/post publication blocker.
- `ENGAGE CREATOR` uses the creator's profile instead of topic search. The
  creator being studied is independent of the logged-in commenting account.
- `ENGAGE CREATOR REFRESH` updates stale known posts for that creator and
  retains all comment-publication duplicate guards.
- `ENGAGE SHADOW` continues through built-in AI analysis, drafting, independent
  AI review, and database storage, but cannot publish.
- `ENGAGE NO-API` is the same gated workflow using the interactive built-in AI
  rather than an external LLM API. It is not a fast track or bypass.
- `ENGAGE LIVE` applies only to a specific stored final response whose exact
  text is shown through `show-response` and then explicitly authorized by the
  same named human using its bound one-time token.

## AUDIT Canonical Flow

```text
social-browser/account preflight
-> collect or refresh requested TikTok evidence
-> verify the exact evidence-ready count
-> built-in AI analysis of every post
-> deterministic aggregation of the complete analysis set
-> calculate provisional portfolio ratings
-> store hash-bound tiktok-audit-report-v1
-> audit_complete
-> stop
```

The last complete `import-analysis` automatically creates the report. The
read-only `audit-report --run-id <run-id>` command validates and displays it.
AUDIT has no response reclassification, drafting, independent response review,
response storage, presentation, approval token, authorization, handoff,
comment publication, showcase publication, or receipt. Its response-type
values are analysis metadata used for portfolio coverage only.

## ENGAGE Canonical Flow

```text
social-browser/account preflight
-> collect requested TikTok evidence
-> verify exact evidence-ready count
-> built-in AI analysis
-> built-in AI draft
-> separate built-in AI critic review
-> store reviewed rendered response
-> show exact response to a named human and issue a bound one-time token
-> record that same human's explicit presentation-bound authorization
-> revalidate browser, evidence, score, and hashes
-> publish each authorized response sequentially
-> store one publication receipt per response
```

### 1. Preflight

Start or reuse the designated social browser. Verify:

- the debugging endpoint is reachable;
- TikTok is logged in;
- the active account handle is resolved and bound to the run;
- the active account exactly matches the configured account, when configured;
- browser-derived credentials remain in memory and are never written to disk;
- the browser will remain running for the workflow.

Never copy credential values into logs or evidence.

The initial preflight covers collection. The later analysis, AUDIT report
aggregation, drafting, critic review, storage, `show-response`, and
authorization stages read only stored evidence and local workflow state, so
they do not revalidate or wait for Profile 7. Revalidate Profile 7 only when
refreshing TikTok evidence and at the publication handoff immediately before a
live submission.

### 2. Collect the Exact Requested Count

For a request of `N` posts, collect until there are exactly `N` unique
evidence-ready TikTok post IDs. A counted record contains the post identity and
URL, creator, caption, current public metrics, transcript/subtitle results,
comments/replies results, collection time, provenance, and an evidence hash.
An explicit unavailable result is valid for content TikTok does not provide;
an unattempted or failed field is not silently treated as complete.

`N` is count-agnostic. The examples use 50 posts, but collection, AI queues,
AUDIT aggregation, authorization, and sequential publication have no hardcoded
50-post maximum.
Requests above 50 continue toward their exact requested count and report
`collection_incomplete: X/N` only for a real, recorded collection constraint.
The related-query discovery budget also scales from `N`; it has no fixed
50-post or 64-query cutoff.

Every production collection attaches
`comments_data/tiktok_master/state/tiktok_master.sqlite` alongside the
project-local workflow database. The default `new_only` policy checks canonical
post IDs against this registry before hydration. A globally known result is
skipped, does not count toward `N`, and causes the request-scaled discovery
frontier to expand for a replacement. Short-lived master-registry leases avoid
concurrent duplicate collection by separate tasks.

For one-account coverage, use creator source mode. It accepts an exact handle or
profile URL and supports either a fixed count or `ALL`:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_maker\state\engage_state.sqlite `
  collect `
  --project maker `
  --creator "@maker" `
  --all-posts `
  --workflow engage `
  --collection-policy new_only `
  --mode shadow `
  --expected-account your_commenting_account
```

The collector binds `@maker` and its stable TikTok identity, walks the profile
post feed until a verified `hasMore=false`, deduplicates pinned/repeated cards,
and preserves video and photo post URLs. It then freezes the inventory and its
hash. Under `new_only`, globally known IDs are removed before hydration; `ALL`
therefore means every new publicly accessible post in that frozen snapshot.
The selected count becomes the exact run target and is not limited to 50.

Every hydrated record is checked back against that frozen ID, URL, owner, and
video/photo type. Captionless photo posts retain their slide count plus an
explicit visual-evidence unavailable outcome; they can be counted without the
AI inventing what an unseen slide contains.

Use `--posts N` instead of `--all-posts` for exactly `N` new posts. If the
terminal profile contains fewer than `N` eligible records, or if TikTok never
provides a terminal frontier, collection records `collection_incomplete` and
AI stages remain locked. A resume reuses the immutable inventory rather than
silently rescanning a different profile snapshot.

For an AUDIT creator run, use the same collection command with
`--workflow audit --mode shadow`. For example:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_maker_audit\state\engage_state.sqlite `
  collect `
  --project maker_audit `
  --creator "@maker" `
  --all-posts `
  --workflow audit `
  --collection-policy new_only `
  --mode shadow `
  --expected-account your_collection_account
```

To revisit known posts on that creator, use a separate fixed-count refresh:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_maker\state\engage_state.sqlite `
  collect `
  --project maker `
  --creator "@maker" `
  --posts 25 `
  --workflow engage `
  --collection-policy refresh_known
```

Creator refresh selects stale master-registry rows for that handle and opens
their saved URLs directly. It does not re-comment posts already blocked by the
active account's publication ledger. `--all-posts` is intentionally limited to
`new_only`; use a fixed count or repeat `--refresh-post-id` for refreshes.

`AUDIT CREATOR REFRESH` changes that example to `--workflow audit` with
`--mode shadow`. Because AUDIT is observational, prior comment-publication
state does not prevent a post from being measured again; the audit neither
clears that ledger nor creates a new comment attempt.

Use `--workflow listen` for LISTEN, `--workflow audit --mode shadow` for AUDIT,
and `--workflow engage` for ENGAGE. A LISTEN run stops after exact collection
and registry sync and rejects `export-analysis`. An AUDIT run permits analysis
export/import, generates its deterministic report, and rejects every response
or publication stage.

`--collection-policy refresh_known` selects stale posts already associated
with the requested topic. The run stores the complete ordered candidate
selection and the absolute `--refresh-stale-before` timestamp. A resume reuses
those exact saved settings. Refresh opens each canonical post URL directly,
reparses TikTok's HTML rehydration item (including its subtitle manifest), and
then refreshes transcript/comments. Broad search discovery is not used.
Cached captions and metrics cannot make an HTML-refresh failure count as fresh
evidence.

Repeat `--refresh-post-id <id>` for an explicit targeted selection. Those IDs
are refreshed without an automatic age filter unless
`--refresh-stale-before` is also provided. With no explicit IDs, the topic
selection defaults to records last observed at least 24 hours before run
creation. Refresh flags are invalid with `new_only`.

Deduplicate by canonical post ID. Replace duplicates, unavailable posts,
and partial failures by continuing discovery and pagination. Do not treat the
number of search cards or attempted URLs as the collected count.

TikTok web search may expose only one page of roughly 12 posts for a single
query. For requests larger than that page, the shared LISTEN/AUDIT/ENGAGE
collector must use target-aware discovery: preserve the complete topic anchor
while expanding into related searches, build an oversized unique candidate
reserve, and request additional discovery rounds when evidence or relevance
checks reject candidates. Deduplicate across every query and stop evidence
collection immediately when the `N`th evidence-ready post is reached.
Increasing `max-pages` alone is not an exact-count strategy, and unrelated
viral posts must never be used to pad the requested count.

If the source is exhausted or access/rate failures prevent completion, store
and report `collection_incomplete: X/N` with reasons, then stop. Analysis and
publication must not run on an underfilled batch.

Each normalized candidate is committed as a durable checkpoint under a fenced
collection-attempt ID. If collection is interrupted, resume the immutable
saved request rather than starting over:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  resume-collect --run-id <run-id>
```

Resume may repair incomplete evidence, but it cannot replace an evidence-ready
record, alter the requested topic/count/account/settings, or commit more than
`N`. Partial checkpoints remain ineligible for analysis. If all `N` records
were committed immediately before a process interruption, resume finalizes the
run from those checkpoints without an unnecessary browser access.

Each evidence-ready local checkpoint also appends an idempotent snapshot and
change summary to the master registry. The local database remains authoritative
for exact-count gates, hashes, AI state, authorization, and receipts. Inspect
global counts with:

```powershell
& $EngagePython engage_tiktok.py master-status
```

### 3. Analyze With the Built-In AI

Only after the whole batch passes the exact-count and evidence gate, the
interactive Codex/Antigravity AI reads each stored evidence packet and records:

- a grounded summary and relevant factual checks;
- evidence completeness and uncertainty;
- `post_quality_score` from 0 to 100;
- a separate `conversation_value_score` from 0 to 100;
- the code-calculated canonical `analysis_score` from the configured bounded
  combination of those components;
- one response type: `positive_support`, `constructive_suggestion`,
  `constructive_correction`, `clarifying_question`, or `skip`;
- `response_opportunity_score` and `grounding_confidence` for a constructive
  type, plus its objective, rationale, correction target when applicable, and
  any blocking risk flags;
- positive eligibility or constructive eligibility and any skip reason; and
- the evidence version/hash used for the decision.

`NO-API` means this interactive analysis replaces an external LLM API. It does
not mean that analysis may be omitted.

To keep large runs practical, AI queues use a compact hash-bound evidence
projection. It retains the caption, metrics, transcript/subtitle results, all
collected comment and reply text and useful thread signals, availability,
provenance, and timestamps while omitting avatars, signed media/share URLs, and
other transport-only payloads. The complete raw packet remains unchanged in
SQLite; its canonical evidence hash is embedded in the projection and remains
the binding input for imports and publication.

Large queues should be partitioned by complete post record and processed across
available AI workers. Results return through one canonical importer. If an
ENGAGE run continues to reply work, reviewers must be independent of the
workers that drafted those responses; AUDIT never enters that review stage.
Never parallelize by splitting one post's evidence or by bypassing a stage.

`blocking_risk_flags` means unresolved risk in publishing the proposed
response itself. Do not use it merely to repeat a weakness in the source post
that the constructive response can safely address; record that weakness in
the rationale and evidence references instead.

### Complete an AUDIT and Stop

AUDIT reuses the canonical, hash-valid analysis queue and importer:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  export-analysis --run-id <run-id> --file analysis-queue.jsonl

& $EngagePython engage_tiktok.py --database <database> `
  import-analysis --run-id <run-id> `
  --file analysis-results.jsonl --actor codex-analysis

& $EngagePython engage_tiktok.py --database <database> `
  audit-report --run-id <run-id>
```

The first two commands operate exactly as they do for ENGAGE analysis, but an
AUDIT import cannot advance a post into reply work. When the last required
analysis is valid, the importer deterministically aggregates the entire
immutable set, stores `tiktok-audit-report-v1`, synchronizes its master-registry
summary, and moves the run to `audit_complete`. `audit-report` validates the
stored analysis/report hashes and displays the result without mutation.

Creator audits use rubric `creator-portfolio-v1`; topic audits use
`topic-portfolio-v1`. Required top-level identity fields are `run_id`,
`schema_version`, `rubric_version`, `analysis_set_hash`, `source_mode`,
`requested_count`, and `analyzed_count`. The report also records inventory and
evidence-ready coverage, response-type distribution, evidence completeness,
observation window, source limitations, and `report_hash`. Its rating formulas
are:

```text
content_quality = round_half_up(mean(post_quality_score) / 10, 1)
engage_suitability = round_half_up(mean(analysis_score) / 10, 1)
conversation_value = round_half_up(mean(conversation_value_score) / 10, 1)
rateable_content_quality =
    round_half_up(mean(post_quality_score where response_type != skip) / 10, 1)
evidence_completeness_percent =
    round_half_up(mean(data_completeness), 1)
```

All analyzed posts receive equal weight, and all available ratings round
half-up to one decimal. `evidence_completeness_percent` is a supporting
diagnostic rather than a rating. `rateable_content_quality` must show both its
non-skip numerator and full analyzed denominator. With no eligible denominator,
store unavailable rather than `0.0`. A verified empty `ALL` inventory can
complete at `0/0` with unavailable ratings; an underfilled fixed request cannot
analyze or create a report.

The report sets `provisional_internal=true` and `not_person_rating=true`.
`content_quality` describes the analyzed content portfolio, not the creator as
a person. `engage_suitability` describes the corpus's canonical analysis
scores, not authorization, expected reply volume, or a public comment rating.
`conversation_value` is a supporting portfolio signal. The non-skip
`rateable_content_quality` is selection-biased, and a positive-support-only
mean is more strongly selection-biased and must be marked as such; neither may
replace overall `content_quality`. Always retain the displayed sample size,
coverage, time window, freshness, rubric, and hashes. Never silently combine
reports from different scopes or rubric versions.

Stop after `audit_complete`. Reclassification, draft/review commands,
`show-response`, authorization, handoff, all comment publishers, and COMMENT
SHOWCASE must reject the audit run. Deterministic aggregation and hash
validation do not constitute an independent AI response review.

### 4. Draft With the Built-In AI

For an eligible post, the built-in AI drafts a contextual, helpful comment in
the configured language. It must be specific to the stored caption,
transcript/subtitles, metrics, and conversation evidence, and it must retain
the configured AI disclosure.

Draft according to `response_type`:

- `positive_support`: add a positive evidence-grounded insight.
- `constructive_suggestion`: add one actionable improvement.
- `constructive_correction`: respectfully qualify or correct one claim using
  stored evidence; address the idea, not the creator.
- `clarifying_question`: ask one good-faith question without presenting
  uncertainty as fact.
- `skip`: do not draft.

Only `positive_support` converts the canonical `analysis_score` to a
one-decimal `/10` rating with decimal round-half-up:

```text
public_rating = round_half_up(analysis_score / 10, 1)
```

For example, `82`, `83`, and `84` become `8.2/10`, `8.3/10`, and `8.4/10`.
Render that value into the positive response now. Constructive responses must
contain no rating placeholder, `/10` rating, or rating metadata.

### 5. Run an Independent AI Review

Run a distinct critic pass, preferably in a separate AI context or sub-agent.
The reviewer compares the final rendered response with the evidence and checks:

- factual grounding and unsupported claims;
- usefulness, tone, language, response-type consistency, and constructive
  safety;
- AI disclosure;
- consistency between the response type, rating policy, and response;
- direct evidence support for a correction, or appropriate uncertainty
  handling for a clarifying question; and
- absence of unresolved placeholders.

A failed review returns to drafting. The drafting pass cannot approve itself,
and AI review approval is not permission to publish.

### 6. Store the Reviewed Final Response

Before any live request, store the exact reviewed response and its audit data
in the database:

- post ID and target URL;
- evidence and analysis hashes/version;
- response type, internal scores, and conditional `public_rating` metadata;
- final rendered response and rendered-text hash;
- analysis, draft, and review results/timestamps; and
- a state that is pending exact-response presentation and explicit live
  authorization.

Do not use direct SQL injection, fabricated analysis state, empty evidence
hashes, or after-the-fact hash synchronization to make a draft appear valid.

### 7. Present the Exact Response

Run the presentation command while its output is actually shown to the named
human who will decide whether to approve it:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  show-response --run-id <run-id> --post-id <post-id> `
  --presented-to <user-identity>
```

The output includes the target URL, response type, applicable rating state,
exact final rendered response, draft and review hashes, `presentation_hash`,
and a one-time `approval_token`. Positive responses show the canonical rating;
constructive responses show no rating. The token is bound to that named human
and exact presentation, including the target, response, and review hashes. A
new `show-response` call replaces the token and invalidates the previous one.

`show-response` only stores evidence that the exact response was presented. It
does not authorize, hand off, publish, or perform any outbound TikTok action.

### 8. Obtain Explicit Live Authorization

Only after the same named human explicitly approves the exact shown response,
record the authorization:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  authorize --run-id <run-id> --post-id <post-id> `
  --authorized-by <user-identity> `
  --draft-hash <exact-draft-hash> `
  --review-hash <exact-review-hash> `
  --presentation-hash <presentation-hash> `
  --approval-token <one-time-approval-token>
```

`--authorized-by` must match `--presented-to`. The presentation hash and token
must match the exact stored response and AI review. Once authorization advances
the response, that token cannot authorize another response.

Requests to listen, gather, analyze, draft, review, test, run `SHADOW`, or run
`NO-API` do not authorize publication. Neither does `show-response` by itself.
An initial expression of live intent or a stored publication ID does not
approve text that had not yet been shown.

### 9. Revalidate and Publish

Immediately before publication, verify again:

- browser connection, TikTok login, and expected account;
- target availability and evidence freshness;
- evidence, analysis, score, and rendered-text hashes;
- independent AI review, the bound presentation, and matching explicit user
  authorization;
- exact response-type/rating consistency and AI disclosure;
- duplicate protection and any explicitly configured publication limits.

Submit only the identified stored response, without editing it in transit.
Store a receipt with the exact submitted text/hash, target, account, timestamp,
and observed result. A failed or uncertain submission remains failed/unknown;
it must not be marked published without evidence.

If multiple exact responses have each been shown, reviewed, and explicitly
authorized, they may be submitted as one requested batch. The publisher still
processes that batch sequentially: one atomic claim, final revalidation,
submission, and receipt per response. There is no hidden five-per-day quota;
an operator may configure a cap or cadence explicitly when desired.

```powershell
& $EngagePython publish_pending.py `
  --database <exact-database> `
  --all-approved `
  --engage-run-id <run-id> `
  --execute
```

The default `--daily-limit 0` means no local daily cap and there is no count
ceiling. A positive daily limit or `--inter-publication-delay` applies only when
the operator supplies it. Batch publication stops on the first unverified
outcome and leaves later rows approved for a later resume; crossing that
failure boundary requires explicit `--continue-on-error`.

If publication-time target, account, evidence, attestation, or freshness checks
fail, mark the publication and ENGAGE post `stale` or `expired` and store an
audit event. Recovery starts from fresh collection/analysis and requires a new
review and user authorization; never repair hashes or reset the old row.

## TikTok Comment Showcase Flow

A confirmed published comment can produce one TikTok photo post showing that
comment. Call this a `COMMENT SHOWCASE`; do not call it a TikTok Repost.
The showcase is a second outbound publication with its own artifacts, review,
presentation, authorization, duplicate fence, and receipt.

```text
confirmed comment receipt
-> exact unique comment-element screenshot and proof
-> API-ready JPEG staged under a verified public HTTPS prefix
-> stored source-evidence context
-> built-in AI caption
-> different built-in AI reviewer/context
-> exact JPEG + caption + source URL + account presentation
-> separate named-human authorization
-> Profile 7/account + hosted-byte + API creator-info preflight
-> atomic local/master claim
-> durable submit intent immediately before photo initialization
-> TikTok PHOTO / DIRECT_POST / PULL_FROM_URL
-> status poll and Profile 7 remote verification
-> local/master confirmed or uncertain receipt
```

The live comment publisher stores its receipt before attempting the screenshot.
Capture failure is therefore a retryable showcase problem, not a failed
comment, and must never cause duplicate comment publication.

The normal comment publisher automatically captures and enqueues after each
confirmed receipt. It also auto-discovers the secret-free
`showcase_auto_prepare.json` beside the scripts. Start from
`showcase_auto_prepare.example.json`; enable it only after the absolute paths,
FFmpeg, and TikTok-verified HTTPS media prefix have been checked. An alternate
file is selected with `--showcase-auto-prepare-config` on
`publish_pending.py`.

Use the read-only resume resolver for `COMMENT SHOWCASE: <publication id>`,
`COMMENT SHOWCASE: <showcase id>`, or `COMMENT SHOWCASE: all pending`:

```powershell
& $EngagePython comment_showcase_status.py --database <database> `
  --publication-id <confirmed-publication-id>

& $EngagePython comment_showcase_status.py --database <database> `
  --showcase-id <showcase-id>

& $EngagePython comment_showcase_status.py --database <database> `
  --all-pending
```

It opens SQLite in read-only/query-only mode, resolves confirmed publication
receipts and showcase jobs, includes confirmed comments whose capture or
enqueue did not finish, and reports the next deterministic, built-in-AI, or
human stage. Its printed commands are guidance only: the resolver never
captures, prepares, drafts, reviews, presents, authorizes, opens Profile 7, or
publishes.

If automatic capture failed, recover the exact artifact without submitting a
new comment:

```powershell
& $EngagePython recover_tiktok_comment_capture.py `
  --database <database> `
  --publication-id <confirmed-publication-id> `
  --receipt-id <confirmed-receipt-id>
```

Recovery reuses Profile 7 only when recapture is necessary, preserves the
confirmed receipt, enqueues idempotently, and resumes deterministic media
preparation when configured.

Use the preparation command before caption drafting:

```powershell
& $EngagePython publish_comment_showcase.py prepare `
  --database <database> `
  --publication-id <confirmed-comment-publication-id> `
  --receipt-id <confirmed-comment-receipt-id> `
  --capture-root <capture-root> `
  --public-directory <served-directory> `
  --public-base-url <TikTok-verified-HTTPS-prefix> `
  --ffmpeg <ffmpeg-executable>
```

Use `tiktok_comment_showcase.py` for `caption-input`, `store-caption`,
independent `review`, `show`, and `authorize`. `show` is valid only when the
exact local image is visibly rendered to the named human alongside the exact
caption, source URL, account, public privacy/comment/music settings, and
hashes. The presentation must also visibly include TikTok's Music Usage
Confirmation consent declaration. The approval token is one-time and binds
the named human's explicit agreement to that declaration and the complete
image, caption, source, account, settings, and hash presentation.

The caption is grounded in the stored reference evidence and exact comment. It
must disclose AI assistance, name the source creator, and include the canonical
source URL exactly once. Store `caption_text_unverified_clickability`: a plain
caption URL is useful attribution, but its TikTok clickability is not
guaranteed.

Dry-run the authorized output first:

```powershell
& $EngagePython publish_comment_showcase.py publish `
  --database <database> `
  --showcase-id <showcase-id> `
  --public-media-base-url <same-verified-HTTPS-prefix>
```

Live mode additionally requires `--execute --token-env <name>`. Never pass a
token value as a CLI argument, read it from a workspace file, print it, place
it in a caption, or persist it in SQLite. The worker uses only the official
TikTok Content Posting API for transport; built-in-AI caption drafting and
review do not call an external LLM API.

Public execution requires `PUBLIC_TO_EVERYONE`; never silently fall back to
private visibility. A confirmed outcome requires `PUBLISH_COMPLETE`, exactly
one returned public ID, and two successful Profile 7 observations of the
matching `/@account/photo/<id>` result. A timeout, absent public ID, failed
remote verification, or exception after submit intent remains `uncertain`
until reconciled.

`--all-authorized` processes the eligible set sequentially and can be scoped
with `--run-id`. The default has no count ceiling or hidden daily quota.
Positive `--max-items` or `--cadence-seconds` values are explicit operator
choices. Stop at the first failed or uncertain result unless the operator
explicitly asks to continue.

Use only the guarded publisher for claim, submit-intent, and outcome changes;
the low-level state CLI intentionally exposes none of those transitions. If a
process stopped after an atomic claim, inspect it without mutation:

```powershell
& $EngagePython publish_comment_showcase.py reconcile `
  --database <database> `
  --showcase-id <showcase-id>
```

For a state still at `reserved`, first verify that no durable submit intent
exists, then explicitly release it to `retryable` with
`--release-reserved --execute`. For `submit_intent` or `uncertain`, never
release or retry. Reconcile the existing TikTok operation with
`--public-media-base-url`, `--token-env`, and `--execute`; add `--publish-id`
only when a crash prevented that existing ID from being stored. Reconciliation
queries status and verifies media, account, and the public post; it never
initializes a new photo post.

Each confirmed showcase target is keyed globally by the source comment
publication attempt. Its remote photo ID is added to `known_post_ids()`, so a
later LISTEN, AUDIT, or ENGAGE run will not recollect the workspace's generated
post as new, and ENGAGE will not comment on it.

## State and Count Rules

The permitted ENGAGE happy-path state order is:

```text
collecting
-> collection_complete
-> analyzed
-> drafted
-> ai_reviewed
-> stored_pending_authorization
-> response_presented_token_issued
-> authorized
-> revalidated
-> published
```

AUDIT has a separate terminal path:

```text
collecting
-> collection_complete
-> analyzed
-> audit_complete
```

LISTEN stops at `collection_complete`. An AUDIT run keeps `drafted`, `reviewed`,
`stored`, `authorized`, and `published` at zero and creates no publication
rows. Its response-type distribution, including `skip`, is analytical report
metadata rather than a queue of replies.

Posts may move to `skipped`, `stale`, or `failed` with a reason at the
appropriate gate. `skipped` is reserved for cases with no safe, grounded, and
useful response opportunity. They may not jump over analysis, drafting,
review, storage, presentation, authorization, or revalidation.

Track `requested`, `unique_collected`, `evidence_ready`, `analyzed`, `drafted`,
`reviewed`, `stored`, `authorized`, `published`, `skipped`, and `failed`.
`status` also reports per-stage telemetry, including recorded operation time
and stage wall time, so browser, collection, AI, human-approval, and publication
latency can be evaluated separately.

The collection count and publication count are intentionally different. A
request for `N` posts must yield `N` evidence-ready records before analysis,
but quality and conversation-value gates may result in anywhere from zero to
`N` publishable responses. Never manufacture comments to meet a publication
quota. Conversely, do not suppress valid feedback with an arbitrary low local
cap. If all `N` responses pass review and the named human explicitly authorizes
all `N` exact texts, the publisher must be able to process all `N`
sequentially, subject only to the same per-response guards and actual platform
outcomes.

## Tool Boundary

Do not run `incremental_project.py` or `run_scraper.py` for any LISTEN, AUDIT,
or ENGAGE shortcut, including `ENGAGE NO-API`. They are legacy bulk campaign
orchestrators.

Use a verified targeted TikTok collector for a supplied URL/video ID, or a
TikTok-only bounded discovery collector that continues until the requested
evidence-ready count is met. Do not rely on hardcoded profile probes, missing
helper scripts, queue-injection shortcuts, manual hash fixes, or automatic
publication from a `live` database flag.

## Compact Requests

```text
LISTEN | topic="3D printing" | platform=tiktok | posts=50
AUDIT | topic="3D printing" | platform=tiktok | posts=50
AUDIT CREATOR | creator="@maker" | platform=tiktok | posts=ALL
AUDIT REFRESH | topic="3D printing" | platform=tiktok | posts=50
AUDIT CREATOR REFRESH | creator="@maker" | platform=tiktok | posts=25
ENGAGE | topic="photography" | platform=tiktok | posts=10
ENGAGE SHADOW | topic="photography" | platform=tiktok | posts=10
ENGAGE NO-API | topic="cybersecurity" | platform=tiktok | posts=25
ENGAGE LIVE | publication_id="<id>"
```

Any platform other than TikTok is out of scope for this workspace.
