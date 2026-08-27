# TikTok PULSE, MUSIC AUDIT, SONIC AUDIT, AUDIT, and ENGAGE

This workspace has three canonical durable TikTok-only workflow modes plus one
quick-look mode. `MUSIC AUDIT` is the official user-facing name for the
music-enriched LISTEN profile: it collects and stores exact evidence plus
terminal TikTok music-declaration and configured-catalog outcomes, while the
persisted workflow value remains `listen`.
`MUSIC AUDIT BACKFILL` applies the current music-evidence schema to eligible
known posts by appending music-only observations; it does not recollect their
metrics, transcripts, or comments.
`SONIC AUDIT` is a separate permission-gated research workflow for comparing
the audible signal of a frozen set of already-known creator posts. It uses
transient media only inside its isolated runner, deletes that media after local
feature extraction, and does not change MUSIC AUDIT's metadata-only boundary.
Its `plan-corpus` command is an offline, query-only planning utility and does
not authorize or execute an audio run. Only `run-plan-batch` may bridge one
validated batch into a separately authorized SONIC run.
The Mirelo Audio-to-MIDI extension is a reserved design and is not implemented
in this release. Mirelo-enabled CLI requests fail closed before run creation;
the local SONIC workflow remains the only executable acoustic path.
`AUDIT` collects,
analyzes, scores, and stores a provisional portfolio report without creating
replies. `ENGAGE` continues from evidence and analysis through drafting and
independent review, and permits guarded publication only after
presentation-bound explicit user authorization for every exact response.
`PULSE` (chat alias `QUICK AUDIT`) is a separate noncanonical, ephemeral sample
that favors speed and never creates a response or publication path.

Older multiplatform LISTEN/reporting code is retained in
`docs/legacy/LEGACY_LISTEN.md`, but it is not an active LISTEN, AUDIT, or ENGAGE execution
path.

The cross-platform MUSIC AUDIT foundation in
`tiktok_scraper/social_music_contract.py`,
`tiktok_scraper/social_music_state.py`, and `social_music_audit.py` can safely
normalize, checkpoint, status, and export already collected Instagram,
Facebook, X, and YouTube metadata. It is collection-only and records unsupported
music/transcript/subtitle surfaces explicitly. It does not download media,
enable a live platform adapter, or add cross-platform analysis, engagement, or
publication. See `docs/contracts/CROSS_PLATFORM_MUSIC_AUDIT.md` for the current capability and
authorization matrix. In this offline substrate, `supported` and `unsupported`
describe accepted importer projections, not verified official-API availability or
authorization.

An independent official-API LinkedIn Page collector lives in
`linkedin_workflow.py`. It can durably collect authorized organization posts
under isolated `listen` or `engage` state, but both LinkedIn values are
collection-only. It neither changes the TikTok shortcuts in this README nor
joins the cross-platform social-music importer. See `docs/contracts/LINKEDIN_WORKFLOW.md`.

`tiktok_one_top_content.py` is a separate read-only discovery bridge for the
TikTok One **Discover top content** page. It stores only canonical TikTok video
links and bounded rank/filter provenance, supports exact 20, 50, 100, or larger
requests by traversing and deduplicating the page's three observed Top 100
rankings, and never retains captions, comments, media, raw page data, or
credentials. A completed link set can be passed sequentially to the existing
one-URL MUSIC AUDIT/LISTEN collector. This does not add a canonical source mode,
AI stage, publication path, or chat shortcut. See
`docs/contracts/TIKTOK_ONE_TOP_CONTENT.md`.

See `docs/contracts/SOURCE_SCOPE.md` for the source-backup boundary, including which files
are active, legacy reference material, or intentionally kept out of Git.

Chat shortcuts:

```text
PULSE: 3D printing, 5 posts
QUICK AUDIT: 3D printing, 5 posts
PULSE CREATOR: @maker, 5 posts
PULSE URL: https://www.tiktok.com/@maker/video/1234567890
MUSIC AUDIT: 3D printing, 50 posts
MUSIC AUDIT CREATOR: @maker, ALL
MUSIC AUDIT URL: https://www.tiktok.com/@maker/video/1234567890
MUSIC AUDIT REFRESH: 3D printing, 20 posts
MUSIC AUDIT CREATOR REFRESH: @maker, 20 posts
MUSIC AUDIT URL REFRESH: https://www.tiktok.com/@maker/video/1234567890
MUSIC AUDIT BACKFILL: 3D printing, ALL
MUSIC AUDIT CREATOR BACKFILL: @maker, ALL
MUSIC AUDIT URL BACKFILL: https://www.tiktok.com/@maker/video/1234567890
SONIC AUDIT CREATOR: @maker, 60 posts
LISTEN: 3D printing, 50 posts
LISTEN CREATOR: @maker, ALL
LISTEN URL: https://www.tiktok.com/@maker/video/1234567890
LISTEN CREATOR REFRESH: @maker, 20 posts
LISTEN URL REFRESH: https://www.tiktok.com/@maker/video/1234567890
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

SONIC AUDIT additionally requires the NumPy and SciPy packages declared in
`requirements.txt`, plus `ffmpeg` and `ffprobe` on `PATH`. `fpcalc` is optional;
when it is unavailable, the checked-in deterministic spectral-landmark
fallback is used and recorded in the feature evidence.
Optional Mirelo execution reads its API key only from the process environment
variable `MIRELO_API_KEY`; do not put this secret in a command, repository
file, plan, manifest, log, database, or export.

## Separate LinkedIn Page collection

LinkedIn collection uses `LINKEDIN_ACCESS_TOKEN` and LinkedIn's official API;
it never starts Profile 7 or another browser. Sources are limited to one
administered organization Page or one exact authorized organization post URN.
There is no topic/member discovery, `ALL`, browser scraping, export, AI,
drafting, approval, or publication path. In particular, LinkedIn
`--workflow engage` still stops after evidence storage.

Use the same required interpreter and place the OAuth token only in the process
environment. For example:

```powershell
$env:LINKEDIN_ACCESS_TOKEN = "<runtime OAuth access token>"
$LinkedInDb = "comments_data\linkedin\linkedin_collection.sqlite3"

& $EngagePython .\linkedin_workflow.py --database $LinkedInDb collect `
  --workflow listen `
  --source organization `
  --organization-urn "urn:li:organization:123456" `
  --posts 25 `
  --run-id "linkedin-listen-page-001"
```

The collector excludes already-known Page posts, freezes the ordered eligible
URN inventory and hash, and completes only with exactly the requested number of
evidence-ready records. An underfilled run is `collection_incomplete: X/N`;
`resume` reuses only that frozen scope and never discovers substitutes. Comment
and reply payloads expire after 48 hours, organization-post payloads after 180
days, and only URN tombstones survive purging. Runtime state stays in the
isolated default database
`comments_data/linkedin/linkedin_collection.sqlite3`. See
`docs/contracts/LINKEDIN_WORKFLOW.md` for exact-post, resume, status, purge, and capability
commands.

## Workflow boundaries

PULSE has a separate quick path:

```text
authenticated Profile 7/account preflight
-> one bounded shallow topic/profile pass or one direct URL
-> one compact built-in-AI analysis batch
-> display noncanonical quick signals and coverage
-> discard
-> stop
```

PULSE does not use canonical exact-count evidence, project/master databases,
run IDs, stored queues/reports, hashes, resume/refresh, drafting, review,
authorization, or publication. It may return `X/N` from its one pass. Its
result cannot be promoted into LISTEN, AUDIT, or ENGAGE.

LISTEN has one terminal sequence:

```text
authenticated social-browser/account preflight
-> collect or refresh TikTok evidence
-> extract terminal platform-declared music metadata
-> perform deterministic configured-catalog enrichment
-> verify exactly N unique evidence-ready posts
-> store hash-bound evidence and synchronize the master registry
-> stop
```

MUSIC AUDIT BACKFILL has a separate durable maintenance sequence:

```text
select and freeze eligible known posts plus their base evidence hashes
-> fetch only current TikTok music metadata when needed
-> resolve eligible Apple tt2dsp IDs and MusicBrainz catalog support
-> append separate hash-bound music observations
-> stop without AI or publication
```

SONIC AUDIT has a separate permission-gated research sequence:

```text
explicit per-run transient-audio authorization
-> freeze an ad hoc exact-creator set, or revalidate and freeze one exact plan batch
-> authenticated Profile 7/account preflight
-> bounded transient media/audio acquisition and decode
-> local deterministic fingerprints/features and comparison
-> optional separately authorized, preflighted, credit-capped Mirelo symbolic
   diagnostic for the same frozen audio
-> guaranteed raw-media cleanup
-> persist only hashes, features, evaluation, and timings in an isolated run
-> optionally validate the completed immutable run offline
-> stop without master mutation, AI-per-post work, or publication
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

Canonical collection never authorizes publication. A LISTEN, AUDIT, or ENGAGE
request for `N` posts succeeds only with `N` unique evidence-ready TikTok post
IDs. If bounded discovery cannot produce `N`, the run records
`collection_incomplete: X/N`, stops, and does not analyze, draft, or publish the
underfilled batch. The common 50-post example is not a ceiling: the pipeline
derives its count from the run and supports larger requests without silently
truncating them to 50. Its related-query discovery plan scales from `N` as
well, rather than stopping at a fixed query frontier.

The requested collection count is not a guaranteed comment quota. Analysis can
skip any post for which no safe, grounded, useful response exists. Conversely,
the publisher does not suppress valid feedback with an arbitrary five-per-day
default: if all requested posts produce reviewed responses and the named human
explicitly authorizes every exact text, all of them can be processed
sequentially with their individual guards and receipts.

Canonical LISTEN can use a topic, one exact creator profile, or one exact
TikTok video/photo URL. Direct URL is a durable one-post LISTEN source and must
not be confused with ephemeral PULSE URL. Initial canonical `--url` support is
LISTEN-only, requires `--posts 1`, and never searches for or substitutes
another post. A creator target is not the logged-in collection or commenting
account: AUDIT or ENGAGE can inspect `@maker` while Profile 7 is authenticated
as `@community_account`. The latter remains the account bound to the run and
to any later ENGAGE authorization and publication checks.

Collection also maintains one workspace-global TikTok post registry. The
default production policy is `new_only`: IDs already known anywhere in the
workspace are removed before expensive metadata/comment hydration, and the
collector keeps discovering replacements until the current run still reaches
exactly `N`. The project-local `engage_state.sqlite` remains the source of
truth for that run's exact-count and later workflow gates.

## 1. Automatic Edge Profile 7 preflight

The executing model does not depend on the user to start a browser.
`engage_tiktok.py collect` automatically starts or reuses Microsoft Edge's
existing `Profile 7` for canonical workflows; `quick_audit_tiktok.py collect`
owns the same preflight for PULSE. Both verify the local connection, TikTok
session, and active TikTok handle before discovery. Profile 7 may be visibly
labelled `Profile 1` in Edge; the profile directory is the canonical identity.
It is expected to contain logged-in sessions for multiple social networks. The
launcher retries one transient startup failure inside the same preflight.
`sonic_audit_tiktok.py run`, `run-plan-batch`, and `resume` own the same
preflight before any transient media access. Its `validate`, `validate-suite`,
`status`, and `export` commands remain local-only; validation writes only its
declared artifact.
`plan-corpus` is also local-only and never starts or attaches to Profile 7.

These low-level commands are available for diagnosis:

```powershell
& $EngagePython social_browser.py start
& $EngagePython social_browser.py status
```

Both commands are idempotent and are restricted to Edge's real user-data root,
profile directory `Profile 7`, in `existing_profile_attach` mode. Active PULSE,
LISTEN, AUDIT, and ENGAGE workflows never create or substitute Edge Default, a
managed/temporary browser profile, or an in-app browser.

A workflow asks for human browser interaction only after Profile 7 was started
or reused successfully and TikTok is genuinely logged out, the account
navigation cannot be resolved after retry, or the active handle is wrong.
`--expected-account` requires an exact handle match. If omitted, the verified
active handle is captured and bound to the run automatically.

Workers open temporary tabs and never close the shared browser.

## Quick audit (PULSE)

Use the required interpreter and one of these shallow collection commands:

```powershell
& $EngagePython .\quick_audit_tiktok.py collect --topic "3D printing" --posts 5
& $EngagePython .\quick_audit_tiktok.py collect --creator "@maker" --posts 5
& $EngagePython .\quick_audit_tiktok.py collect `
  --url "https://www.tiktok.com/@maker/video/1234567890"
```

The command prints an ephemeral `snapshot` plus one compact `analysis_input`.
The interactive built-in Codex/Antigravity model analyzes every sampled row in
one batch. It then passes a JSON object containing `snapshot` and `analyses` to
the pure validator through standard input:

```powershell
$PulseBundleJson | & $EngagePython .\quick_audit_tiktok.py report `
  --actor codex-pulse-analysis
```

`$PulseBundleJson` must contain serialized JSON text, not a formatted PowerShell
object. The result shows requested, sampled, analyzed, rated, and unrated counts;
per-post source links; evidence omissions; confidence; limitations; and three
equal-weight `/10` signals: `quick_content_signal`,
`quick_interest_signal`, and bounded-80/20 `quick_overall_signal`. These are
quick sample signals, not canonical AUDIT portfolio ratings or public-comment
ratings.

For speed, PULSE uses only directly returned caption/description, current
public metrics, and any direct visual description. It omits deep audiovisual
interpretation, transcripts/subtitles, comment text, replies, global-new
deduplication, adaptive replacements, and terminal profile inventory. A row
with metrics but no semantic text stays sampled and is explicitly `unrated`.
There is no hidden post-count ceiling, but PULSE makes only one bounded page,
so large targets are likely partial and are no longer a flash operation.

Every result carries this boundary:

```text
NON-CANONICAL, EPHEMERAL SNAPSHOT — sample-based; not a full AUDIT, not a creator/person rating, not publication eligibility, and not comparable across runs.
```

PULSE does not write audit evidence/results to project or master databases and
cannot accept `ALL`, refresh, resume, database, draft, review, authorization,
handoff, or publication arguments. Use a fresh canonical AUDIT when durable,
deep, reproducible scoring is required.

## MUSIC AUDIT sources and music enrichment (`workflow=listen`)

LISTEN accepts exactly one canonical source: a topic, an exact creator, or one
exact TikTok video/photo URL. It collects caption, metrics,
transcript/subtitles, comments/replies, and platform-declared music metadata,
then performs deterministic configured-catalog enrichment before committing
the evidence hash. Music enrichment is collection, not built-in-AI analysis;
LISTEN still stops after evidence storage and master-registry synchronization.

```powershell
# Topic
& $EngagePython .\engage_tiktok.py `
  --database comments_data\project_music_topic\state\engage_state.sqlite `
  collect `
  --project music_topic `
  --topic "3D printing" `
  --posts 20 `
  --max-comments 100 `
  --workflow listen `
  --collection-policy new_only `
  --mode shadow

# Creator
& $EngagePython .\engage_tiktok.py `
  --database comments_data\project_music_creator\state\engage_state.sqlite `
  collect `
  --project music_creator `
  --creator "@maker" `
  --posts 20 `
  --max-comments 100 `
  --workflow listen `
  --collection-policy new_only `
  --mode shadow

# One globally-new exact URL
& $EngagePython .\engage_tiktok.py `
  --database comments_data\project_music_url\state\engage_state.sqlite `
  collect `
  --project music_url `
  --url "https://www.tiktok.com/@maker/video/1234567890" `
  --posts 1 `
  --max-comments 100 `
  --workflow listen `
  --collection-policy new_only `
  --mode shadow

# Refresh that exact known URL
& $EngagePython .\engage_tiktok.py `
  --database comments_data\project_music_url\state\engage_state.sqlite `
  collect `
  --project music_url `
  --url "https://www.tiktok.com/@maker/video/1234567890" `
  --posts 1 `
  --max-comments 100 `
  --workflow listen `
  --collection-policy refresh_known `
  --mode shadow
```

For topic `new_only`, known IDs are replaced through continued discovery. For
creator `new_only`, fixed `N` or `ALL` is derived from the verified terminal
profile inventory. For URL `new_only`, the fixed target is the only candidate:
if it is globally known, the run reports `collection_incomplete: 0/1` and does
not substitute another post. URL `refresh_known` refreshes that exact known ID,
bypasses the automatic 24-hour cutoff unless an explicit cutoff is supplied,
and rejects a target missing from the master registry. `ALL` is valid only for
creator `new_only`.

The initial required catalog provider is MusicBrainz. Every post stores a
terminal TikTok declaration status and a terminal MusicBrainz outcome:
`matched`, `ambiguous`, `not_found`, `unsupported`, `unavailable`,
`rate_limited`, or `provider_error`. `matched` maps to
`identity_status=catalog_correlated`; it is metadata support, not acoustic
verification. `not_found` means a successful query returned no usable
candidates, while the three provider-failure statuses remain distinct.
Post duration and declared music duration remain separate. Generic
`original sound` metadata produces `unsupported` and stays unresolved rather
than being presented as proof that the uploader created the audible recording.
If TikTok's structured API response also supplies `matched_song` (or its
`matched_pgc_sound` fallback), MUSIC AUDIT stores that separately as
`platform_contained_recording`. A complete title+artist declaration becomes
the MusicBrainz query input and is labeled API-derived—not acoustically
verified. Safe DSP linkage IDs produce a `partial` contained-track status when
title/artist are absent; raw DSP tokens, play URLs, and artwork URLs are never
retained.

When `tt2dsp` includes a validated platform `1` Apple song ID, MUSIC AUDIT
resolves that exact ID through Apple's public lookup in the Indonesian
storefront. It stores only title, artist, album, duration, release date, genre,
explicitness, provider ID, provenance, terminal status, and result hash in a
separate `tt2dsp_resolution`. The original TikTok contained declaration remains
`partial`; a successful resolver result is catalog correlation, not acoustic
recognition. A retained platform `3` Spotify ID is provenance only until an
authorized full-metadata adapter is configured.
Native-language variants of “original sound” are handled primarily through
TikTok's structured `music.original`/`isOriginal` flag, not a translation list.
The displayed-title matcher is only a fallback when that flag is missing.
Omitting `--music-catalog` selects the current default `musicbrainz`; supplying
`--music-catalog musicbrainz` makes the same frozen run setting explicit.

The evidence retains up to five structured MusicBrainz candidates, their
recording/artist identifiers, ISRCs, durations, releases, provider scores,
deterministic match reasons, cache-hit/circuit provenance, and result hashes.
It does not retain signed audio/artwork URLs, download audio, run acoustic
recognition, scrape lyrics, or make music-fit, lyrical, sentiment, or causal
claims. TikTok declaration statuses are `available`, `partial`,
`not_provided`, and `unavailable`. Explicit unavailable and provider-failure
outcomes are terminal, so "complete pull" means every required attempt finished
with a recorded outcome, not that every field or identity was available.
Actual MusicBrainz request starts are serialized across concurrent workspace
projects through the master registry with a 1.05-second minimum interval;
content-addressed in-memory cache hits consume no request slot. A rate refusal
extends the shared cooldown from `Retry-After`, or a conservative fallback.
Apple tt2dsp lookups are likewise serialized workspace-wide at a 3.05-second
minimum interval and never retain preview, artwork, or audio URLs.

No separate AI or queue-enrichment script runs after LISTEN. In particular,
do not run `legacy/one_off_state_mutators/enrich_music_data.py` against an exported canonical queue; canonical
music enrichment belongs before evidence hashing and checkpoint storage.

### Backfill music evidence on older known posts

Use `music_backfill_tiktok.py` to upgrade music evidence already stored in the
workspace-global master registry. Backfill is reusable across older projects;
it is not tied to one creator such as BankBCA. It selects records whose latest
music evidence is missing, older than the target schema, or in a frozen
retryable terminal state. Current terminal records are skipped unless
`--force` is explicitly requested.

This differs from the two collection policies:

- `new_only` discovers and stores globally new full post evidence.
- `refresh_known` appends a full current snapshot, including metrics,
  transcript/subtitles, comments/replies, and music.
- backfill operates only on known rows and appends music observations without
  updating the base snapshot, master `last_seen`, or global-new status.

Backfill all eligible known posts for a creator:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database comments_data\tiktok_master\state\tiktok_master.sqlite `
  run `
  --project bankbca_music_backfill `
  --creator "@bankbca" `
  --all-eligible `
  --target-schema tiktok-music-evidence-v3
```

Use `--limit N` instead of `--all-eligible` for a bounded topic or creator
selection. The other exact known-post scopes are `--topic`, `--url`, and one or
more repeatable `--post-id` values:

```powershell
& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run --project topic_music_backfill `
  --topic "banking" --limit 100

& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run --project url_music_backfill `
  --url "https://www.tiktok.com/@maker/video/1234567890" --all-eligible

& $EngagePython .\music_backfill_tiktok.py `
  --master-database $MasterDatabase `
  run --project selected_music_backfill `
  --post-id 1234567890 --post-id 1234567891 --all-eligible
```

Exactly one scope and exactly one of `--all-eligible` or `--limit N` are
required. URL and post IDs must already exist in the master registry; backfill
never turns an unknown target into new collection. Optional run controls are
repeatable `--retry-status`, `--force`, `--expected-account`, and
`--max-pages`. `--target-schema tiktok-music-evidence-v3` is the current
explicit schema setting.

For topic, URL, and explicit post-ID work items, direct HTML remains the primary
music-metadata transport. The narrow fallback applies only to frozen exact URL
or post-ID targets whose direct HTML attempt failed. It inventories each
affected exact owner once, accepts only the row matching the frozen post ID,
owner, and video/photo type, and ignores all unrelated profile rows. Topic
scope cannot use the fallback. A missing target in a verified terminal profile
inventory becomes explicitly `unavailable`; a nonterminal frontier cannot
prove absence, so a still-missing target leaves the run incomplete and
resumable. No new flag is needed, and no fallback may collect metrics,
comments, transcripts/subtitles, audio, or AI analysis.

Resume, inspect, or export the immutable run:

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

The run freezes each post ID, canonical URL, base snapshot identity/hash,
target schema, provider set, retry rules, and selection order. It revisits only
the TikTok metadata needed for the music declaration, contained recording, and
safe `tt2dsp` linkage; eligible Apple IDs are resolved before MusicBrainz is
queried. Each terminal result has a separate music-observation timestamp and
hash. Removed, private, inaccessible, or metadata-missing posts may complete as
explicitly `unavailable` without blocking the batch.

No command refreshes metrics, transcripts, subtitles, comments, replies, or
visual evidence, overwrites an old snapshot, changes `last_seen`, or creates AI
analysis, scores, drafts, approvals, publication rows, or receipts. `status`
and `export` are read-only and do not attach to Profile 7 or call providers.

### Plan a positive-pair SONIC corpus offline

`plan-corpus` evaluates current registry evidence and writes a deterministic
no-clobber plan without creating a SONIC run. Global `--master-database` and
`--output-root` options precede the subcommand. Choose exactly one repeatable
scope, `--creator` or exact `--post-id`; `--exclude-run-id` is repeatable only
with creator scope. The four planning limits and `--file` are required:

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

Repeat `--creator` for a multi-creator plan, or instead repeat `--post-id` for
an exact all-or-nothing feasibility scope. The latter rejects exclusions and
fails if any requested post is missing or ineligible. Eligibility is limited
to hash-verified public videos with exact Apple `apple_itunes_lookup` `tt2dsp`
resolution from completed, current v3 music-backfill evidence. No generic
original-sound label, unresolved match, older schema, incomplete run, or
unverified binding is promoted into ground truth.

The `tiktok-sonic-audit-corpus-plan-v1` artifact binds its exact scope and
targets, candidate/evidence hashes, excluded run IDs/hashes and post IDs,
repeated Apple reference groups, and deterministic single-creator batches of
at most 60 posts. The target file must not already exist and cannot overlap the
master or SONIC run-state paths. Planning performs no browser/TikTok or catalog
request, media/audio access, AI analysis, master mutation, or run creation. It
does not grant transient-audio permission.

### Run one exact planned corpus batch

Each batch is a separate SONIC run and requires fresh, explicit non-AI human
permission for that exact plan/batch selection:

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

`--authorize-transient-audio` records permission already granted for this new
batch run; the plan itself, a prior run, or permission for another batch is not
authorization. Omit optional `--expected-account` only if no exact active
Profile 7 account was requested. When supplied, it is normalized, frozen in the
run manifest, and must be honored on resume.

Before any browser access or run creation, the runner verifies the plan schema
and hash; source-scope and set hashes; every candidate, group, and batch hash
and relationship; the requested batch ID; its master-database binding; and the
current master snapshot, base evidence, v3 music observation, exact Apple
resolution, and latest-observation binding for every candidate. Changed,
missing, stale, mismatched, or tampered evidence fails closed.

Execution freezes the selected batch's exact order: one creator, 1-60 unique
public videos, with no discovery, addition, dropping, or substitution. It
preserves the typed Apple track/provider/storefront/ID/label/title/artist and
`tt2dsp` basis, together with plan/batch/group/candidate hashes and positions,
through the manifest and feature records. Acoustic similarity cannot upgrade
that external label.

Only one run may exist for a plan hash plus batch ID. A repeated
`run-plan-batch` invocation is rejected with the existing run ID; use ordinary
`resume --run-id`, then `status`, completed-run `validate`, `validate-suite`, or
`export` as appropriate without restating the plan. Media/audio stays bounded
and transient with finally-path cleanup, the master registry remains read-only,
and no built-in semantic AI runs. Plan-bound evaluation remains
`exploratory_only`.

### Optional Mirelo Audio-to-MIDI diagnostic

> **Implementation status:** reserved and disabled. This release has no Mirelo
> transport adapter, performs no Mirelo upload, and rejects Mirelo-enabled run
> creation. The contract below is retained as design documentation only; do not
> pass these flags or configure an API key for the current release.

Mirelo is optional and disabled by default. To add its symbolic transcription
to either a new ad hoc `run` or a new exact `run-plan-batch`, append all three
Mirelo flags to the already-required local audio authorization:

```powershell
--authorize-transient-audio `
--mirelo-audio-to-midi `
--authorize-mirelo-upload `
--mirelo-max-credits $MireloMaxCredits
```

The same flag set is valid on both creation commands. It is invalid on
`plan-corpus`, `resume`, `status`, `validate`, `validate-suite`, and `export`.
The user must separately authorize third-party upload for that exact frozen
run and confirm they have the rights needed to submit its audio under the
current [Mirelo Terms](https://mirelo.ai/terms). Local transient-audio
permission, a previous run/batch, a plan, or an available API key does not
grant upload permission. The upload decision, provider/model configuration,
and positive `--mirelo-max-credits` run ceiling are hash-bound in the manifest;
resume can only continue those frozen values and candidates.

Before each upload, the adapter uses Mirelo's preflight credit/ETA estimate and
checks the estimate plus committed credits against the frozen run ceiling. It
does not upload when preflight fails or the ceiling would be exceeded. It may
submit only the bounded decoded audio for the exact frozen post currently
being processed—never discovered or replacement content. A provider failure
is an explicit symbolic outcome and does not invalidate or relax the primary
local SONIC feature result.

Tell the authorizing user that Mirelo may retain provider-side input/output
assets for up to 24 hours; deleting the local temporary files does not prove
immediate remote deletion. Locally, the uploaded audio and returned MIDI,
MusicXML, raw/structured notes, instrument tracks, provider payloads, and
job/result/download URLs are transient and are never stored. The run may keep
only a bounded sanitized hash-bound summary containing provider/model/config
provenance, the input-audio hash binding, terminal status, preflight/credit
scalars, non-reconstructable aggregate symbolic diagnostics, timings, and
result hashes.

This summary is supporting evidence only. It cannot change the primary
`recording_score`, similarity threshold, catalog identity, or cluster ground
truth, and cannot justify identity, genre, mood, lyrics, ownership, creator-
quality, or engagement-causality claims. Offline `status` and `export` read the
stored sanitized summary without calling Mirelo, using the key, resolving URLs,
or spending credits. Provider tests are mocked only and use synthetic fixtures;
they never upload live audio or consume credits. See Mirelo's current
[Audio-to-MIDI API documentation](https://mirelo.ai/api-docs#audio-to-midi)
and [model documentation](https://mirelo.ai/models/audio-to-midi).

### Run a SONIC AUDIT creator pilot

SONIC AUDIT is for local acoustic comparison, not song-name discovery.
Ad hoc v1 `run` takes only an exact creator and 1-60 public video posts
already present in the workspace-global master registry. It does not accept
topic, URL, explicit ID, `ALL`, or photo/carousel rows; search the live profile;
or replace a frozen post. `run-plan-batch` above is the only separate exact
plan-bound execution shape. An ad hoc run reads the master registry without
modifying it and creates a separate run under
`comments_data/sonic_audit_runs/<run-id>/`.

Transient media/audio acquisition requires explicit human permission for each
new run. After receiving that permission, execute the 60-post BankBCA pilot as:

```powershell
& $EngagePython .\sonic_audit_tiktok.py `
  --master-database comments_data\tiktok_master\state\tiktok_master.sqlite `
  run `
  --project bankbca_sonic_pilot `
  --creator "@bankbca" `
  --posts 60 `
  --authorize-transient-audio
```

`--authorize-transient-audio` attests to permission already granted by a non-AI
user. The model must never add it merely because audio would improve an audit,
and permission from another MUSIC/SONIC AUDIT does not carry forward.
`run`, `run-plan-batch`, and `resume` optionally accept `--expected-account` to
require an exact active Profile 7 account; that account is independent of
`--creator`. Keep the default output root for production runs.

Run creation freezes all 60 ordered post IDs, canonical URLs, creators, base
evidence snapshot IDs/hashes, selection method, master path, and authorization.
If 60 valid known exact-owner rows are unavailable,
the runner fails instead of silently reducing the pilot or discovering
replacements.

Each completed feature record separately stores and hash-binds its effective
feature schema, algorithm, and configuration. Transport receipts bind fixed v1
conversion provenance and timing. Resume only under the unchanged deployed
code, dependencies, and configuration; never mix a changed implementation into
an existing run.

For a separate nonoverlapping labelled-first extension, add repeatable
`--exclude-run-id $PriorCompleteRun` to a new `run`. Each prior run must be
complete and bind the same creator/master database. The runner freezes its
exclusion hashes, keeps the 60-post maximum, and requires new transient-audio
authorization.

The deterministic 60-post selector targets 21 usages from repeated exact Apple
references, 30 distinct singleton Apple references, and 9 unresolved/original
posts. A short bucket is filled from remaining eligible exact-owner pools
without duplicates, and the realized mix is reported. No live post is added as
a replacement. This is a stratified validation sample, not a random sample or
representative audit of the creator's full portfolio. Its findings must be
reported against the realized pilot composition and labelled support.

Resume, inspect, and export that immutable run with:

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

The optional `validate` stage accepts only a completed immutable run and needs
no new transient-audio authorization. It performs no browser, TikTok,
media/audio, AI, provider, or master-database access. It leaves `report.json`
and `state.json` unchanged and writes only the run-local
`statistical_validation.json`. That file uses wrapper schema
`tiktok-sonic-audit-statistical-validation-v1` with a nested
`sonic-statistical-validation-v1` evaluation and binds the manifest, source
report, and ordered feature-record set hashes.

Validation uses reference-label-disjoint out-of-fold calibration, retains full
descriptive positive/negative score distributions and a complete threshold
sweep, and reports reference-grouped bootstrap intervals plus finite-support
Wilson diagnostics. The current BankBCA pilot has eight repeated reference IDs
and ten positive pairs, so the recommendation is `exploratory_only`, not a
production-ready operating threshold. `status` and `export` remain read-only;
export includes the statistical-validation artifact when present.

`validate-suite` requires at least two ordered unique complete, nonoverlapping
runs with the same creator, master binding, and feature contract. It binds all
source hashes and the combined feature set, then writes the no-clobber
`tiktok-sonic-audit-statistical-validation-suite-v1` file requested by
`--file`. It performs no browser, media/audio, master, or AI access. Combined
results remain `exploratory_only`.

Each frozen URL is opened only after Profile 7 preflight and its returned post
ID/owner must match. Download and decode are bounded by time, bytes, duration,
media type, and format. Raw video, extracted audio, PCM, stems, and temporary
spectrograms are deleted in a `finally` path on success, error, cancellation,
and resume repair. Completion requires a zero-persistent-raw-media check.
Signed URLs, cookies, headers, authorization data, and raw payloads are neither
logged nor stored.

V1 is sequential. Defaults are 64 MiB source media, 8 MiB decoded WAV, and 180
seconds of audio per post, plus a 3,600-second run budget. Hard caps are 128 MiB
source, 16 MiB WAV, and 300 seconds per post. HTML is capped at 8 MiB, a request
at 30 seconds, transcode at 90 seconds, and redirects at three. Limit failures
remain terminal outcomes and trigger the same cleanup.

A safe per-item transport failure is stored as `unavailable` after cleanup and
the run continues without replacement. A preflight/account, programming, or
run-wide budget failure leaves pending items and `sonic_incomplete` for resume.
All 60 immutable `completed`/`unavailable` records produce `sonic_complete`.

Durable artifacts contain only the immutable manifest/bindings, per-post
terminal checkpoints, audio/content hashes, safe duration/quality scalars,
local fingerprints, numeric feature vectors or embeddings, similarity and
cluster results, thresholds, timings, software/model provenance, evaluation,
and result hashes. SONIC AUDIT never updates the master database, `last_seen`,
canonical evidence/music records, global-new state, analysis queues, approval,
or publication history.
The verified export envelope uses `tiktok-sonic-audit-export-v1`; it packages
the hash-bound manifest, state, terminal feature records, report, and optional
statistical-validation artifact without adding media.
Feature records wrap `sonic-feature-v1`; evaluation uses
`sonic-similarity-report-v1` and `sonic-cluster-stability-v1` inside the
hash-bound `tiktok-sonic-audit-report-v1` report.

Similarity results are internal evidence about this frozen corpus. They cannot
identify a catalog recording, artist, release, or ISRC on their own. A name may
be shown only when the frozen base evidence independently contains a hash-bound
TikTok/Apple/MusicBrainz identity, with provenance and uncertainty retained.
Unresolved is valid; a generic original-sound label is not proof of ownership or
identity.
The v1 baseline uses deterministic local DSP fingerprints and numeric feature
vectors. It does not download or run Essentia, CLAP, Shazam, or an AcoustID
catalog lookup, so no external model/API identity is implied.

The pilot measures coverage, outcome counts, cleanup, feature/schema hashes,
total and per-stage/per-post runtime, similarity/cluster coverage, abstention,
and cluster stability. With enough independent same-recording labels,
post-disjoint evaluation reports labelled/distinct/repeated reference support,
evaluable/resolved/unresolved query counts, abstention rate, Recall@1,
Recall@5, pair TP/FP/TN/FN, precision, true-positive rate, and false-match rate
at the selected threshold. Otherwise identity accuracy is `not_evaluable`.
Cluster stability uses mean/minimum/maximum adjusted Rand index plus its
resampling basis; it must never be presented as identity accuracy, and
within-post windows are not independent test examples. This processing is local
deterministic computation, not 60 AI calls, lyrical/semantic analysis, creator
scoring, or proof that audio caused engagement. Accuracy and timing are measured run outputs, not estimates;
they remain unavailable until the pilot reaches terminal records and builds its
report.
Recorded monotonic timing fields are `html_fetch_ms`, `media_download_ms`,
`inspect_transcode_ms`, `processor_ms`, and `total_item_ms` per post, plus
`preflight_ms`, `attach_ms`, `transport_ms`, and `total_run_ms` for the run.

### Export a completed MUSIC AUDIT evidence packet

A completed LISTEN run can export one safe semantic JSONL row per collected
post:

```powershell
& $EngagePython .\engage_tiktok.py `
  --database comments_data\project_music_topic\state\engage_state.sqlite `
  export-evidence `
  --run-id <run-id> `
  --file comments_data\project_music_topic\music_audit_evidence.jsonl
```

The global `--database` option must precede `export-evidence`. The command
requires `workflow=listen`, status `collection_complete`, no active collection
attempt, and an exact requested/evidence-ready count. It verifies every raw
evidence JSON/hash, post-ID binding and ready flag, plus creator inventory state
when applicable, before atomically writing the file.

Each `tiktok-listen-evidence-export-v1` row includes run/project/source/policy
identity, post ID, canonical evidence hash, compact projection hash, and a safe
semantic `evidence_packet`. The packet retains captions, metrics,
transcript/subtitle results and segments, compact comments/replies,
availability/provenance, and sanitized TikTok music plus MusicBrainz
outcomes/candidates, identity status, catalog result hash, and music evidence
hash. Signed media/share/audio/artwork URLs, avatars, and other transport-only
payloads are omitted.

Export reads stored local evidence and writes only the named artifact. It never
starts or revalidates Profile 7, invokes AI, changes workflow counters/status,
or mutates project/master state. The exported JSONL is not an analysis queue,
AUDIT promotion, authorization, or publication eligibility.

For Gemini/Antigravity and other guarded MUSIC AUDIT execution, use the
operator skill rather than composing the raw command. It creates one dedicated
semantic folder per run, named from the optional explicit run label, actual
policy/source/target/cardinality, timestamp, and microseconds. For example, an
explicit test uses
`project_music_audit_test_new_topic_music_1p_<timestamp>_<microseconds>`;
ordinary topic, creator, and URL runs omit the label and derive their identity
from the real scope. Each new folder contains
`state/<artifact-stem>_state.sqlite`, `<artifact-stem>_handoff.json`,
`<artifact-stem>_ledger.jsonl`, complete-only `<artifact-stem>_evidence.jsonl`,
`<artifact-stem>_review.json`, and `<artifact-stem>_review.md`. The stem is
compact, semantic, unique to the run, and recorded in the handoff. Existing
layout-v1 runs retain their original fixed names and must not be renamed.
Names such as `results.json`, `audit_results.json`, and project-level CSV
exports remain noncanonical.

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
terminal platform-music declaration, terminal configured-catalog enrichment,
availability metadata, provenance, observation time, provider/result hashes,
and the canonical evidence hash. Explicit unavailable, zero-result,
insufficient-metadata, ambiguity, and provider-failure outcomes are preserved
rather than fabricated as successful identification.

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
stages, and resume can never exceed the original requested `N` or change the
frozen source target, collection policy, catalog-provider set, or refresh
selection.

`LISTEN` uses the same collector with `--workflow listen`. It completes
deterministic music enrichment before the evidence checkpoint, synchronizes the
verified hash-bound snapshot to the master registry, and then stops.
`export-analysis` and every later AI/response/publication stage are refused for
a LISTEN run. The read-only `export-evidence` exception only serializes the
already stored safe semantic evidence packet and does not advance workflow
state.

A LISTEN run cannot be promoted or analyzed in place. A later AUDIT must create
its own canonical AUDIT or AUDIT REFRESH run. When that run collects or
refreshes the same post, its evidence and compact AI projection retain the
stored music declaration, catalog candidates, terminal outcomes, and hashes.
AUDIT must preserve identity uncertainty, make no acoustic or lyrical claim
without corresponding authorized evidence, and must not infer that music
caused engagement.

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
text, thread signals, sanitized platform music declarations, catalog candidates
and terminal outcomes, provider/result hashes, availability, provenance, and
timestamps while dropping avatars, signed media/share/audio/artwork URLs, and
other transport-only fields. The raw packet stays unchanged in SQLite. Each
projection embeds the canonical raw evidence hash and has its own projection
hash. Catalog correlation is not acoustic verification, and absent authorized
lyrics evidence cannot support a lyrical-fit claim.
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
& $EngagePython quick_audit_tiktok.py --help
& $EngagePython sonic_audit_tiktok.py --help
& $EngagePython engage_tiktok.py --help
& $EngagePython engage_tiktok.py --database <database> status --run-id <run-id>
& $EngagePython engage_tiktok.py --database <database> audit-report --run-id <run-id>
& $EngagePython -m pytest -q `
  tests/sonic/test_sonic_contracts.py tests/sonic/test_sonic_features.py `
  tests/sonic/test_sonic_audio_transport.py tests/sonic/test_sonic_audit_cli.py `
  tests/sonic/test_sonic_evaluation.py tests/sonic/test_sonic_suite_validation.py `
  tests/sonic/test_sonic_corpus_planning.py
& $EngagePython -m pytest -q
```

The status output includes per-stage timing telemetry so collection, AI work,
human approval waiting, and publication overhead can be reviewed separately.

Full operating rules are in `AGENTS.md`; worked examples and JSONL checkpoint
contracts are in `WORKFLOWS.md`.
