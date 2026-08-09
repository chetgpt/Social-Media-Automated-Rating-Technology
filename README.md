# TikTok LISTEN, AUDIT, and ENGAGE

This workspace has three active TikTok-only workflow modes. `LISTEN` collects
and stores exact evidence. `AUDIT` collects, analyzes, scores, and stores a
provisional portfolio report without creating replies. `ENGAGE` continues from
evidence and analysis through drafting and independent review, and permits
guarded publication only after presentation-bound explicit user authorization
for every exact response.

Older multiplatform LISTEN/reporting code is retained in
`LEGACY_LISTEN.md`, but it is not an active LISTEN, AUDIT, or ENGAGE execution
path.

Chat shortcuts:

```text
LISTEN: 3D printing, 50 posts
LISTEN CREATOR: @maker, ALL
AUDIT: 3D printing, 50 posts
AUDIT CREATOR: @maker, ALL
AUDIT REFRESH: 3D printing, 50 posts
AUDIT CREATOR REFRESH: @maker, 25 posts
ENGAGE: 3D printing, 50 posts
ENGAGE CREATOR: @maker, ALL
```

On this workstation, initialize the Python command once per PowerShell session:

```powershell
$EngagePython = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
```

Use `& $EngagePython` in place of `py -3` in the examples below. Bare
`python` is intentionally not used because workspace path resolution can
shadow it, and this machine's `py -3` launcher is not registered.

## Workflow boundaries

LISTEN has one terminal sequence:

```text
authenticated social-browser/account preflight
-> collect or refresh TikTok evidence
-> verify exactly N unique evidence-ready posts
-> store and synchronize evidence
-> stop
```

AUDIT has one terminal sequence:

```text
authenticated social-browser/account preflight
-> collect or refresh TikTok evidence
-> verify exactly N unique evidence-ready posts
-> built-in AI analysis of every post
-> deterministic complete-set aggregation and provisional ratings
-> store hash-bound tiktok-audit-report-v1
-> audit_complete
-> stop
```

ENGAGE continues through the response workflow:

```text
authenticated social-browser/account preflight
-> collect TikTok evidence
-> verify exactly N unique evidence-ready posts
-> built-in AI analysis
-> built-in AI draft
-> independent built-in AI review
-> store exact reviewed text and hashes
-> show exact response to a named human and issue a bound one-time token
-> explicit non-AI user authorization
-> guarded per-response handoff
-> browser/account/evidence/hash revalidation
-> publish each authorized exact stored text sequentially
-> one receipt and ENGAGE-state update per response
```

Collection never authorizes publication. A request for `N` posts succeeds only
with `N` unique evidence-ready TikTok post IDs. If bounded discovery cannot
produce `N`, the run records `collection_incomplete: X/N`, stops, and does not
analyze, draft, or publish the underfilled batch. The common 50-post example is
not a ceiling: the pipeline derives its count from the run and supports larger
requests without silently truncating them to 50. Its related-query discovery
plan scales from `N` as well, rather than stopping at a fixed query frontier.

The requested collection count is not a guaranteed comment quota. Analysis can
skip any post for which no safe, grounded, useful response exists. Conversely,
the publisher does not suppress valid feedback with an arbitrary five-per-day
default: if all requested posts produce reviewed responses and the named human
explicitly authorizes every exact text, all of them can be processed
sequentially with their individual guards and receipts.

Collection can use either a topic or one exact creator profile. A creator
target is not the logged-in collection or commenting account: AUDIT or ENGAGE
can inspect `@maker` while Profile 7 is authenticated as
`@community_account`. The latter remains the account bound to the run and to
any later ENGAGE authorization and publication checks.

Collection also maintains one workspace-global TikTok post registry. The
default production policy is `new_only`: IDs already known anywhere in the
workspace are removed before expensive metadata/comment hydration, and the
collector keeps discovering replacements until the current run still reaches
exactly `N`. The project-local `engage_state.sqlite` remains the source of
truth for that run's exact-count and later workflow gates.

## 1. Automatic Edge Profile 7 preflight

The executing model does not depend on the user to start a browser.
`engage_tiktok.py collect` automatically starts or reuses Microsoft Edge's
existing `Profile 7`, then verifies the local connection, TikTok session, and
active TikTok handle before collecting anything. Profile 7 may be visibly
labelled `Profile 1` in Edge; the profile directory is the canonical identity.
It is expected to contain logged-in sessions for multiple social networks.
The launcher retries one transient startup failure inside the same preflight
before marking the run browser-blocked.

These low-level commands are available for diagnosis:

```powershell
& $EngagePython social_browser.py start
& $EngagePython social_browser.py status
```

Both commands are idempotent and are restricted to Edge's real user-data root,
profile directory `Profile 7`, in `existing_profile_attach` mode. Active
LISTEN, AUDIT, and ENGAGE workflows never create or substitute Edge Default, a
managed/temporary browser profile, or an in-app browser.

A workflow asks for human browser interaction only after Profile 7 was started
or reused successfully and TikTok is genuinely logged out, the account
navigation cannot be resolved after retry, or the active handle is wrong.
`--expected-account` requires an exact handle match. If omitted, the verified
active handle is captured and bound to the run automatically.

Workers open temporary tabs and never close the shared browser.

## 2. Collect exactly the requested number

Choose and retain one database path for the entire run:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_demo\state\engage_state.sqlite `
  collect `
  --project demo `
  --topic "3D printing" `
  --posts 50 `
  --max-comments 100 `
  --workflow engage `
  --collection-policy new_only `
  --mode shadow `
  --expected-account your_tiktok_handle
```

Each counted record contains a canonical ID/URL, creator, analyzable post text,
metrics, terminal transcript/subtitle result, terminal comments result,
availability metadata, provenance, observation time, and evidence hash.
Explicit unavailable and zero-result outcomes are preserved rather than
reported as successful content collection.

To collect one creator's complete new public inventory, replace `--topic` and
`--posts` with `--creator` and `--all-posts`:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_maker\state\engage_state.sqlite `
  collect `
  --project maker `
  --creator "https://www.tiktok.com/@maker" `
  --all-posts `
  --max-comments 100 `
  --workflow engage `
  --collection-policy new_only `
  --mode shadow `
  --expected-account your_commenting_account
```

Creator discovery uses the authenticated profile-post feed, binds the exact
handle plus stable creator identity, supports both video and photo posts, and
deduplicates pinned/repeated entries by post ID. `ALL` is resolved only after a
verified terminal profile response (`hasMore=false`). The terminal inventory,
selected ordered IDs, identity, observation time, and hash are stored before
post hydration so a resume cannot drift to a different snapshot.

Each evidence checkpoint must match its frozen post ID, owner, URL, and media
type. A captionless carousel stores its slide count and an explicit terminal
visual-evidence outcome; downstream AI may use available metrics/comments or
skip it, but cannot invent visual details that were not collected.

With the default `new_only` policy, known master-registry IDs are excluded;
`--all-posts` therefore means every globally-new publicly accessible post in
the frozen profile snapshot. The resulting number becomes the exact collection
target and may be zero or greater than 50. A non-terminal/stalled profile is
incomplete, not empty, and cannot unlock analysis. Use `--posts N` for exactly
`N` new posts from the same creator.

Refresh known creator posts separately with `--creator @maker --posts N
--collection-policy refresh_known`. The selection is creator-scoped, opens
saved canonical URLs directly, appends evidence snapshots/deltas, and keeps the
already-commented ledger intact. For an explicit subset, repeat
`--refresh-post-id`. `--all-posts` is reserved for `new_only` collection.

Candidates are committed incrementally under a fenced attempt ID. If a large
collection is interrupted, resume its immutable request and continue from the
durable checkpoints:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  resume-collect --run-id <run-id>
```

Ready records cannot be overwritten, partial checkpoints do not unlock AI
stages, and resume can never exceed the original requested `N`.

`LISTEN` uses the same collector with `--workflow listen`. It synchronizes the
verified evidence to the master registry and then stops; `export-analysis` is
refused for a LISTEN run.

`AUDIT` uses `--workflow audit --mode shadow`. It shares the exact-count and
master-registry collection contract, permits the existing analysis
export/import commands, automatically stores its report when the complete
analysis set is imported, and then stops at `audit_complete`. For example:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_audit_3d_printing\state\engage_state.sqlite `
  collect `
  --project audit_3d_printing `
  --topic "3D printing" `
  --posts 50 `
  --max-comments 100 `
  --workflow audit `
  --collection-policy new_only `
  --mode shadow `
  --expected-account your_tiktok_handle
```

For `AUDIT CREATOR`, use the same command with `--creator @maker` and either
`--posts N` or `--all-posts`. `ALL` retains the creator inventory rules above
and means all globally-new posts in the frozen terminal snapshot, not a coded
maximum.

In chat, use `LISTEN REFRESH: <topic>, <count>` for a collection-only
incremental update, `AUDIT REFRESH: <topic>, <count>` when the refreshed set
should be analyzed and reported, or `ENGAGE REFRESH: <topic>, <count>` when it
should continue through the SHADOW response stages. The exact-owner AUDIT
variant is `AUDIT CREATOR REFRESH: <@handle>, <count>`. Refresh never removes
the account/post publication history; an AUDIT refresh is observational and
may analyze posts that ENGAGE would exclude from new comment work.

To refresh known posts instead of discovering new IDs, create a separate run:

```powershell
& $EngagePython engage_tiktok.py `
  --database comments_data\project_demo\state\engage_state.sqlite `
  collect `
  --project demo `
  --topic "3D printing" `
  --posts 50 `
  --workflow listen `
  --collection-policy refresh_known `
  --refresh-stale-before "2026-07-28T12:00:00+07:00"
```

The selected registry IDs, candidate records, and absolute staleness cutoff
are stored with the run and cannot change on `resume-collect`. Refresh uses
each saved canonical URL directly, reparses TikTok's HTML metadata and
subtitle item, and then refreshes transcript/comments. Cached caption or
metrics cannot make a failed refresh evidence-ready.

For particular posts, repeat `--refresh-post-id <TikTok-post-id>` on the
`refresh_known` command. Explicit IDs are refreshed regardless of age unless
you also provide `--refresh-stale-before`; topic-only refresh defaults to posts
last observed at least 24 hours earlier. Refresh flags are rejected by
`new_only`.

For an AUDIT refresh, change the example to `--workflow audit --mode shadow`.
For a creator-scoped AUDIT refresh, also replace `--topic` with
`--creator @maker`. AUDIT refresh shortcuts require a fixed `--posts N`; use
repeatable `--refresh-post-id` values for a specific immutable subset.

The master registry defaults to
`comments_data/tiktok_master/state/tiktok_master.sqlite`. Inspect it without
opening a project run:

```powershell
& $EngagePython engage_tiktok.py master-status
```

Use the global `--master-database <path>` option only when the entire run,
including every resume, will use that same alternate registry.

Do not use `incremental_project.py` or `run_scraper.py` for LISTEN, AUDIT, or
ENGAGE. They are campaign-wide legacy LISTEN orchestrators. TikTok collection
may use browser-backed TikTok web requests inside the authenticated session;
`NO-API` specifically means no external LLM API is used for AI analysis,
drafting, or review. AUDIT analysis likewise always uses the interactive
built-in AI.

## 3. Analyze with the built-in AI

Export analysis work:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  export-analysis --run-id <run-id> --file analysis-queue.jsonl
```

The interactive built-in AI analyzes only the supplied evidence and returns
separate `post_quality_score` and `conversation_value_score` values, confidence,
grounded policy fields, and evidence references. Import those results:

For large runs, the exported AI packet is a compact semantic projection: it
keeps captions, metrics, transcripts/subtitles, all collected comment and reply
text, thread signals, availability, provenance, and timestamps while dropping
avatars, signed media/share URLs, and other transport-only fields. The raw
packet stays unchanged in SQLite. Each projection embeds the canonical raw
evidence hash and has its own projection hash.
Large queues may be partitioned by complete post record across available AI
workers, and all results return through the canonical hash-validating importer.
If an ENGAGE run later continues to reply work, independent reviewers must not
review their own drafts. AUDIT never enters that review stage.

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  import-analysis --run-id <run-id> `
  --file analysis-results.jsonl --actor codex-analysis
```

For an AUDIT run, the final complete, hash-valid analysis import automatically
aggregates the immutable analysis set, stores `tiktok-audit-report-v1`, and
sets the run to `audit_complete`. Verify and display it without changing state:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  audit-report --run-id <run-id>
```

The deterministic report uses rubric `creator-portfolio-v1` for creator runs
and `topic-portfolio-v1` for topic runs. Its top-level identity includes
`run_id`, `schema_version`, `rubric_version`, `analysis_set_hash`,
`source_mode`, `requested_count`, and `analyzed_count`. It also binds the
response-type distribution, evidence completeness and observation window, and
`report_hash`. Its one-decimal `/10` portfolio values use equal weight per
analyzed post and decimal round-half-up:

```text
content_quality = round_half_up(mean(post_quality_score) / 10, 1)
engage_suitability = round_half_up(mean(analysis_score) / 10, 1)
conversation_value = round_half_up(mean(conversation_value_score) / 10, 1)
rateable_content_quality =
    round_half_up(mean(post_quality_score where response_type != skip) / 10, 1)
evidence_completeness_percent =
    round_half_up(mean(data_completeness), 1)
```

`content_quality` rates only the analyzed content portfolio, never the creator
as a person. `engage_suitability` summarizes the canonical analysis scores; it
is not permission to comment, an expected publication rate, or the public
rating used in a positive-support reply. `evidence_completeness_percent` is a
supporting evidence diagnostic, not a rating. `rateable_content_quality` must
show its non-`skip` numerator, all-analysis denominator, and coverage. It is
selection-biased, and any positive-support-only average is even more
selection-biased. A missing denominator produces an unavailable rating, not
`0.0`. The report sets `provisional_internal=true` and
`not_person_rating=true`; any positive-support subset metric must be marked
selection-biased. Every value remains bound to the report's exact scope,
freshness, rubric, and hashes.

AUDIT stops here. It may not reclassify responses, export or import drafts,
export or import reviews, store a final response, run `show-response`,
authorize, hand off, publish a comment, or create a COMMENT SHOWCASE. Those
commands must reject `workflow=audit`; analysis response classifications are
portfolio measurement metadata only.

## 4. Draft and independently review ENGAGE responses

Export and import drafts:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  export-drafts --run-id <run-id> --file draft-queue.jsonl

& $EngagePython engage_tiktok.py --database <database> `
  import-drafts --run-id <run-id> `
  --file draft-results.jsonl --actor codex-drafter
```

Every draft must explicitly disclose AI involvement. A `positive_support`
draft contains `{PUBLIC_RATING}` exactly once; code deterministically converts
the canonical 0–100 analysis score to a one-decimal `/10` rating using decimal
round-half-up, renders it before storage, and hashes the exact final text. For
example, scores `82`, `83`, and `84` become `8.2/10`, `8.3/10`, and `8.4/10`.
Constructive suggestions, constructive corrections, and clarifying questions
contain no rating placeholder, `/10` rating, or enabled rating metadata.

Export and import a separate critic review:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  export-reviews --run-id <run-id> --file review-queue.jsonl

& $EngagePython engage_tiktok.py --database <database> `
  import-reviews --run-id <run-id> `
  --file review-results.jsonl --actor codex-independent-reviewer
```

The reviewer must differ from the drafter and pass grounding, usefulness, tone,
language, AI-disclosure, response-type consistency, and the applicable rating
policy. Review does not grant permission to publish.

`ENGAGE SHADOW` stops after reviewed responses are stored.

## 5. Show, authorize, and hand off live responses

Live mode must have been selected at collection time. First, show the exact
reviewed response to the named human who will make the decision:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  show-response --run-id <run-id> --post-id <post-id> `
  --presented-to <user-identity>
```

The command prints the target URL, response type, applicable rating state,
exact final response, draft and review hashes, `presentation_hash`, and a
one-time `approval_token`.
Run it only when that exact output is actually shown to the named human.
`show-response` only stores the presentation record; it does not authorize,
hand off, publish, or make any outbound TikTok request.

The approval token is bound to that presentation, including the named human,
target, exact response, and review hashes. Re-running `show-response` replaces
the token and invalidates the previous one. After that same human explicitly
approves the shown response, bind authorization to the presentation:

```powershell
& $EngagePython engage_tiktok.py --database <database> `
  authorize --run-id <run-id> --post-id <post-id> `
  --authorized-by <user-identity> `
  --draft-hash <exact-draft-hash> `
  --review-hash <exact-review-hash> `
  --presentation-hash <presentation-hash> `
  --approval-token <one-time-approval-token>

& $EngagePython engage_tiktok.py --database <database> `
  handoff --run-id <run-id> --post-id <post-id>
```

`--authorized-by` must identify the same human named by `--presented-to`. A
token can authorize only its exact presentation and becomes unusable after the
response advances from reviewed to authorized.

The handoff prints the exact database and a dry-run publisher command. Inspect
that single publication first:

```powershell
& $EngagePython publish_pending.py `
  --database <exact-database> `
  --publication-id <publication-id>
```

Only the explicit `--execute` flag enables submission:

```powershell
& $EngagePython publish_pending.py `
  --database <exact-database> `
  --publication-id <publication-id> `
  --execute
```

After every response in one run has separately passed presentation,
authorization, and handoff, process every still-approved response in that run:

```powershell
& $EngagePython publish_pending.py `
  --database <exact-database> `
  --all-approved `
  --engage-run-id <run-id> `
  --execute
```

There is no local count ceiling and the default `--daily-limit 0` is unlimited.
Optional positive `--daily-limit` and `--inter-publication-delay` values can be
supplied explicitly. The batch fails closed on the first unverified outcome,
leaving later approved rows available for resume; continuing past an error
requires explicit `--continue-on-error`.

Immediately before text entry, the publisher revalidates the active TikTok
account, exact-count run, evidence, analysis, rating, independent review, user
authorization, freshness deadline, duplicate state, any explicitly configured
limit, and complete handoff attestation. It atomically claims one row, submits
the exact stored text, and records the target, account, text/hash, rating,
timestamp, and verification result. An authorized batch repeats that operation
sequentially for every row. An ambiguous submission becomes `uncertain` and
requires manual reconciliation; it is never silently retried.

If evidence, hashes, target identity, account, or freshness fails revalidation,
both the publication row and ENGAGE post are marked `stale` or `expired` with
an audit event. Recovery requires fresh collection/analysis, a new independent
review, and new user authorization; do not reset the old row by hand.

## 6. Publish a TikTok Comment Showcase

Each confirmed comment publication now attempts to capture the exact published
comment element and enqueue a separate `COMMENT SHOWCASE` job. The comment
receipt is committed first: if screenshot capture or showcase preparation
fails, the original comment remains confirmed and is never retried because of
that auxiliary failure.

The standard `publish_pending.py` route performs that capture automatically.
It also auto-discovers `showcase_auto_prepare.json` beside the scripts. Copy
`showcase_auto_prepare.example.json` to that name, replace the local paths and
HTTPS prefix, verify that the prefix is registered for the TikTok developer
application and serves the directory without redirects, and only then set
`enabled` and `public_media_verified` to `true`. The file must contain no
token, API key, cookie, password, or other credential. An alternate file can
be selected with `--showcase-auto-prepare-config` on `publish_pending.py`.

This second post uses TikTok's official Content Posting API. `NO-API` still
means that analysis, response drafting, showcase-caption drafting, and both AI
reviews use the interactive built-in AI rather than an external LLM API. It
does not prohibit the TikTok API from transporting an already reviewed and
separately authorized final photo post.

Resolve the `COMMENT SHOWCASE` shortcut without changing workflow state:

```powershell
& $EngagePython comment_showcase_status.py --database <exact-database> `
  --publication-id <confirmed-comment-publication-id>

& $EngagePython comment_showcase_status.py --database <exact-database> `
  --showcase-id <showcase-id>

& $EngagePython comment_showcase_status.py --database <exact-database> `
  --all-pending
```

This command opens SQLite read-only, resolves publication IDs to their
confirmed receipt and showcase job, lists orphaned confirmed comments in
`--all-pending`, and reports the next deterministic, built-in-AI, or human
stage with its exact safe command or required inputs. It never captures,
prepares, drafts, reviews, presents, authorizes, opens Profile 7, or publishes.

Before preparation, configure:

- a TikTok developer application with `video.publish` permission and an
  audited client if the result must be public;
- an operator-managed OAuth access token for the exact Profile 7 TikTok
  account;
- a public HTTPS directory on a TikTok-verified domain or URL prefix, serving
  files without redirects; and
- FFmpeg. On this workstation it is available at
  `C:\ffmpeg-2023-09-07-git-9c9f48e7f2-full_build\bin\ffmpeg.exe`.

Prepare the exact screenshot as a reviewed 1080-by-1920 JPEG before drafting
its caption:

```powershell
& $EngagePython publish_comment_showcase.py prepare `
  --database <exact-database> `
  --publication-id <confirmed-comment-publication-id> `
  --receipt-id <confirmed-comment-receipt-id> `
  --capture-root <project-capture-root> `
  --public-directory <directory-served-by-the-verified-domain> `
  --public-base-url "https://media.example.com/tiktok/showcases" `
  --ffmpeg "C:\ffmpeg-2023-09-07-git-9c9f48e7f2-full_build\bin\ffmpeg.exe"
```

If the confirmed comment exists but its automatic screenshot failed, recover
that auxiliary artifact without republishing the comment:

```powershell
& $EngagePython recover_tiktok_comment_capture.py `
  --database <exact-database> `
  --publication-id <confirmed-comment-publication-id> `
  --receipt-id <confirmed-comment-receipt-id>
```

Recovery starts/reuses Profile 7 only when it must revisit TikTok. If the exact
screenshot is already attached, it enqueues and prepares idempotently without
browser access. With the workspace config enabled, both normal capture and
recovery stop automatically at `publish_media_ready`.

The interactive built-in AI then uses `caption-input`, drafts a caption, and a
different built-in-AI identity and context reviews it:

```powershell
& $EngagePython tiktok_comment_showcase.py --database <exact-database> `
  caption-input --showcase-id <showcase-id>

& $EngagePython tiktok_comment_showcase.py --database <exact-database> `
  store-caption --showcase-id <showcase-id> `
  --caption-file <caption-file> `
  --drafted-by codex-showcase-drafter `
  --draft-context-id <unique-draft-context>

& $EngagePython tiktok_comment_showcase.py --database <exact-database> `
  review --showcase-id <showcase-id> `
  --reviewer codex-showcase-independent-reviewer `
  --review-context-id <different-review-context> `
  --decision-file <review-decision-json>
```

The caption must be grounded in the stored source-post evidence and published
comment, disclose AI assistance, include `@creator`, and contain the canonical
source-post URL exactly once. That URL is stored as caption text; TikTok may
not render an arbitrary caption URL as a clickable link.

Next, `show` must be run only while the model actually displays the exact JPEG,
caption, source URL, posting account, public privacy/comment/music settings,
TikTok's Music Usage Confirmation consent declaration, and returned hashes to
the named human. The human's explicit approval binds that exact consent
declaration together with the image, caption, account, settings, and hashes.
The comment's earlier approval cannot approve this new post:

```powershell
& $EngagePython tiktok_comment_showcase.py --database <exact-database> `
  show --showcase-id <showcase-id> --presented-to <user-identity>

& $EngagePython tiktok_comment_showcase.py --database <exact-database> `
  authorize --showcase-id <showcase-id> `
  --authorized-by <same-user-identity> `
  --presentation-hash <presentation-hash> `
  --approval-token <one-time-token> `
  --media-sha256 <shown-publish-JPEG-hash> `
  --caption-hash <shown-caption-hash> `
  --review-hash <shown-review-hash>
```

Inspect the authorized job without a network submission:

```powershell
& $EngagePython publish_comment_showcase.py publish `
  --database <exact-database> `
  --showcase-id <showcase-id> `
  --public-media-base-url "https://media.example.com/tiktok/showcases"
```

Only `--execute` enables publication. Prefer an environment variable supplied
through the operator's secret-management process; the command receives the
variable name, not the token value:

```powershell
& $EngagePython publish_comment_showcase.py publish `
  --database <exact-database> `
  --showcase-id <showcase-id> `
  --public-media-base-url "https://media.example.com/tiktok/showcases" `
  --token-env TIKTOK_SHOWCASE_ACCESS_TOKEN `
  --execute
```

Immediately before submission, the worker starts/reuses Profile 7, binds the
active handle to API `creator_info`, verifies the hosted JPEG bytes and hash,
and atomically claims both local and master ledgers. It permits only
`PUBLIC_TO_EVERYONE`, records submit intent immediately before the API photo
initialization request, polls to `PUBLISH_COMPLETE`, requires exactly one
public post ID, and verifies that post twice through Profile 7. Anything
ambiguous after submit intent remains `uncertain` and is not retried.

Interrupted showcase attempts have one guarded recovery route. A dry run shows
the active state and permitted action:

```powershell
& $EngagePython publish_comment_showcase.py reconcile `
  --database <exact-database> --showcase-id <showcase-id>
```

A `reserved` attempt has no durable submit intent and can be explicitly
released with `--release-reserved --execute`. A `submit_intent` or `uncertain`
attempt must instead reconcile the existing TikTok publish using
`--public-media-base-url`, `--token-env`, and `--execute`; supply
`--publish-id` only when the ID was not stored before the interruption.
Reconciliation never submits a new photo. Raw claim, submit-intent, and outcome
transitions are not available through the state CLI.

To process an arbitrary authorized set sequentially, use `--all-authorized`
and optionally `--run-id`. There is no coded 50-item or low daily limit;
`--max-items` and `--cadence-seconds` apply only when explicitly supplied. A
batch stops at the first failed or uncertain result by default.

## Commands and tests

```powershell
& $EngagePython engage_tiktok.py --help
& $EngagePython engage_tiktok.py --database <database> status --run-id <run-id>
& $EngagePython engage_tiktok.py --database <database> audit-report --run-id <run-id>
& $EngagePython -m pytest -q
```

The status output includes per-stage timing telemetry so collection, AI work,
human approval waiting, and publication overhead can be reviewed separately.

Full operating rules are in `AGENTS.md`; worked examples and JSONL checkpoint
contracts are in `WORKFLOWS.md`.
