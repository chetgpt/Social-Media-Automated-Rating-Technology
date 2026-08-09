# Campaign Specifications

Before creating or running a campaign, follow
[`NEW_CAMPAIGN_RUNBOOK.md`](NEW_CAMPAIGN_RUNBOOK.md). It is the required
operator sequence for specification setup, social-browser authentication,
bounded validation, full collection, and final coverage review.

Start with `template.json`, save one specification per client or campaign, and
use a new `project` value when the scope or data contract changes materially.

```powershell
python incremental_project.py `
  --campaign-spec campaign_specs\client_campaign.json `
  --social-browser
```

## Input Groups

- `keywords.core`: exact subject, brand, title, or campaign anchors.
- `keywords.hashtags`: hashtag variants, with or without `#`.
- `keywords.entities`: people, products, venues, or partners paired with core
  anchors by default.
- `keywords.optional`: ambiguous category terms paired with core anchors by
  default, which broadens recall without running the generic term alone.
- `keywords.campaign`: slogans, activations, and distinctive creative phrases.
- `keywords.exclusions`: recurring false-positive concepts used by relevance
  scoring.
- `queries`: explicit cross-platform or platform-specific searches.
- `accounts`: owned, partner, cast, publisher, or other named account sources.
- `date_windows`: one or more inclusive ISO-8601 publication windows.

X accepts `x` as the canonical platform name and `twitter` as an input alias.
Account handles become `from:` searches automatically. X `recent` search covers
the last seven days; use `--x-search-mode all` only when the developer app has
full-archive access. Keep the bearer token out of the specification and supply
it through `X_BEARER_TOKEN` or `--x-bearer-token-file`.

Set `collection.discovery_candidates_per_source` higher than
`collection.videos_per_source` when date, relevance, or known-content gates are
enabled. Candidate discovery collects metadata only; the smaller video limit is
applied after those gates and controls expensive comment extraction. A value of
`0` selects the orchestrator default of at least 20 candidates or five times the
post limit. When `videos_per_source` is unlimited, discovery is also unlimited.

Use `discovery.max_sources_per_platform` as a permanent source cap only when
the campaign deliberately limits scope. For a temporary smoke test, prefer the
CLI `--max-sources-per-platform` option so the saved specification still
describes the intended full collection.

## Profile Enrichment

Set `profiles.enabled` to `true` to enrich queued creators and comment authors
after all platform runs finish and before reports are compiled. Official APIs
are attempted first. With `profiles.browser_fallback: true`, the runner can use
the shared authenticated social browser when a platform API is unavailable.
`limit_per_run` bounds profile requests and `cache_days` controls successful
profile refreshes.

Profile location is stored as public or self-declared profile evidence, not as
audience geography. Pronouns and explicit age statements can be preserved from
profile text, but the pipeline does not infer gender, ethnicity, or other
demographics from names, photos, or pronouns. Audience age, gender, and
geographic distributions require first-party analytics access and remain
aggregate.

## Native-Text Analysis

The template enables `analysis.mode: "shadow"`. Evidence packets use only
platform-provided post text, captions, descriptions, transcripts/subtitles,
comments, replies, and metrics. They never trigger media download, speech
recognition, or OCR.

Use `provider: "manual"` or `"codex"` to produce an analysis queue without a
separate model API call. Use `provider: "gemini"` only when `GEMINI_API_KEY` is
configured and model usage is intended. Fact-check verdicts require source
URLs. AI supplies rubric subscores and evidence; the configured weighted
formula calculates the final internal score. The reusable default is
`social-review-v1`. It scores post quality separately from conversation value,
combines them at 80% and 20%, and caps comment influence at ten points. The
original post must independently clear the positive threshold. Client-specific
dimensions may override the default rubric.

`native-text-v4` requires an explicit AI publication decision. Positive-only
mode stores every completed internal analysis but creates a public draft only
when the deterministic score is at least `70`, the AI assessment is positive,
and the decision names both a specific strength and an actionable
recommendation. Mixed, negative, harmful, and not-scorable posts quietly
abstain. Videos without a platform transcript or subtitle remain provisionally
scored internally and cannot receive a public rating. Code converts an eligible
internal score into a one-decimal 1-10 rating and replaces `{PUBLIC_RATING}` in
the AI draft. Raw scores, confidence, completeness, and rubric state remain
private. The default `auto` language policy follows the post and audience while
retaining an explicit AI disclosure. Publication also requires fresh
evidence, an unchanged evidence hash, automation disclosure, human approval,
and duplicate protection. This creates drafts and audit records only. It does
not authorize or execute public comments.

## Storage

The reusable template enables verified Google Drive archival. Its `storage`
section controls the remote root, optional desktop mount check, backlog
reconciliation, compiled/log uploads, database snapshots, and whether verified
raw runs are deleted locally. Keep OAuth secrets and tokens out of campaign
specifications; use `.google_drive\client_secret.json`, the setup command in the
root README, or the `GOOGLE_DRIVE_*` environment variables.

Use `storage.raw_run_archive_timing: "final"` for broad multi-source campaigns
so scraping and SQLite ingestion are not blocked after each individual query or
account. The final archive step still uploads, resumes, and verifies every raw
run through the same ledger. Use `"immediate"` when every source must be copied
to Drive before the next source starts.

Set `storage.required` to `true` when new collection must stop if Drive is
unavailable. This is recommended with `delete_local_after_upload: true`: it
prevents the runner from accumulating more raw data after archival has failed.
The `account_hint` should be the email mounted at `storage.mount_path`, which
guards against consenting with a different Google account.

## Collection Contract

Every source receives a stable key. Each source run writes raw files and an
immutable candidate observation. The candidate ledger merges observations by
canonical platform content ID while retaining every source key. Comment and
content records are then upserted into the project SQLite database, engagement
metrics receive timestamped snapshots, and compiled files are regenerated.

The project `compiled\latest\coverage.json` file is the collection QA report.
Do not mark a run report-ready while configured sources are pending or failed.
Use marginal-new ratios to identify exhausted query variants and metadata
completeness to identify platform extractors that need refinement. No public
scraper can prove literal platform-wide 100% recall; this workflow instead
makes configured-source coverage, overlap, exclusions, and failures measurable.
