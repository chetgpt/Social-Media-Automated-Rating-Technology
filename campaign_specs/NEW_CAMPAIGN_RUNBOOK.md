# New Campaign Runbook

Use this runbook for every new campaign. The production entry point is
`incremental_project.py`; do not use `run_scraper.py` or
`run_full_pipeline.py` as the primary campaign runner.

## Required Run Order

For a normal campaign containing TikTok, Instagram, Facebook, or X:

1. Create and validate a new campaign specification.
2. Let the runner start or verify the designated social browser.
3. Confirm the required platform accounts are logged in.
4. Run a bounded smoke test.
5. Review coverage, raw-run logs, and extraction quality.
6. Run the full campaign.
7. Review coverage again before treating the output as report-ready.

The campaign runner automatically starts or reuses the saved designated social
browser for TikTok, Instagram, Facebook, and X. The same running browser is
reused for sequential platform workers and later campaigns.

YouTube-only and `--compile-only` runs do not normally require the social
browser. A platform using its own CDP URL, persistent profile, storage-state
file, or suitable official API route may also use that explicitly configured
authentication instead.

## 1. One-Time Machine Setup

From the repository root:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

If Google Drive archival will be used, complete the machine authorization
before the campaign:

```powershell
python google_drive_setup.py `
  --account "drive-account@example.com" `
  --mount-path "I:\My Drive" `
  --project "setup_check"
```

Do not put OAuth tokens, browser cookies, bearer tokens, passwords, or API
secrets in a campaign JSON file.

## 2. Create a New Campaign Specification

Copy the template and give the new campaign its own filename:

```powershell
$Campaign = "client_campaign"
Copy-Item campaign_specs\template.json "campaign_specs\$Campaign.json"
```

Edit the new JSON and verify every item below:

- `name`: human-readable campaign name.
- `project`: unique, stable storage name for this campaign and data contract.
- `timezone`: timezone used for campaign dates and reports.
- `platforms`: only the platforms actually required.
- `keywords.core`: exact topic, brand, title, or campaign anchors.
- `keywords.hashtags`: relevant hashtag variants.
- `keywords.entities`: people, products, venues, or partners.
- `keywords.optional`: ambiguous terms that should be paired with a core term.
- `keywords.campaign`: slogans or distinctive activation phrases.
- `keywords.exclusions`: known recurring false positives.
- `queries`: explicit cross-platform or platform-specific searches.
- `accounts`: owned, partner, publisher, talent, or other account sources.
- `date_windows`: inclusive publication windows with timezone offsets.
- `collection`: discovery, post, comment, refresh, and report limits.
- `relevance`: anchor requirements, thresholds, and review behavior.
- `profiles`: whether public profile enrichment is enabled and bounded.
- `storage`: either a verified Drive configuration or an intentional local-only
  configuration.

Use a new `project` value when the scope, date contract, client, or meaning of
the collected dataset changes materially. Reuse the same project only for true
incremental updates to the same campaign.

### Storage Decision

For verified Google Drive archival, keep:

```json
{
  "provider": "google-drive",
  "required": true
}
```

Confirm `account_hint`, `mount_path`, OAuth credentials, and the Drive token are
valid on the current machine.

For an intentional local-only campaign, set:

```json
{
  "provider": "none",
  "mount_path": "",
  "required": false
}
```

Do not leave the template's required Drive configuration unchanged on a
machine where Drive has not been configured.

## 3. Validate the Specification

Run the schema normalizer before opening any platform:

```powershell
$Spec = "campaign_specs\client_campaign.json"
python -c "from campaign_spec import load_campaign_spec; s=load_campaign_spec(r'$Spec'); print('project=', s['project']); print('platforms=', ','.join(s['platforms']))"
```

Stop and correct any schema, timezone, date-window, platform, or relevance
threshold error before continuing.

## 4. Verify the Social Browser

The first browser-backed pipeline command automatically opens or reuses the
saved Edge Profile 7 designation. A manual preflight remains available:

```powershell
python social_browser.py status
```

If status says it is not running, the campaign runner will start it. You can
also run `python social_browser.py start` directly when login maintenance is
needed. Confirm that status reports every required browser-backed platform as
logged in:

- TikTok
- Instagram
- Facebook
- X

Keep the project Edge window running throughout collection. Workers open
temporary tabs, disconnect after their source run, and do not close the shared
profile.

`social_browser.py status` confirms the live Profile 7 CDP session and known
cookie names without printing cookie values. A server can still revoke a cookie,
so treat an extractor authentication response as authoritative and refresh that
platform login when needed.

## 5. Run a Bounded Smoke Test

Never begin a new campaign with unlimited collection. Start with a small number
of sources, posts, and comments:

```powershell
$Spec = "campaign_specs\client_campaign.json"
python incremental_project.py `
  --campaign-spec $Spec `
  --max-sources-per-platform 2 `
  --videos 5 `
  --comments 10 `
  --no-direct-refresh
```

For a YouTube-only campaign, no social browser is started unless profile
enrichment needs its browser fallback or `--social-browser` is supplied
explicitly. Use `--no-social-browser` for an intentional non-browser transport.

The smoke test must complete before the full run. Do not treat an exit code
alone as proof of collection quality.

## 6. Smoke-Test Acceptance Checks

Open:

```text
comments_data\project_<project>\compiled\latest\coverage.json
comments_data\project_<project>\compiled\latest\summary.json
comments_data\project_<project>\compiled\latest\dataset_manifest.json
comments_data\project_<project>\compiled\latest\posts.jsonl
comments_data\project_<project>\compiled\latest\comments.jsonl
comments_data\project_<project>\compiled\latest\analysis_summary.json
comments_data\project_<project>\compiled\latest\analysis_queue.jsonl
comments_data\project_<project>\compiled\latest\publication_queue.jsonl
comments_data\project_<project>\logs\
comments_data\project_<project>\raw_runs\
```

Confirm:

- Expected platforms and sources are present.
- Smoke-test sources are not unexpectedly `failed`.
- A successful source that found zero candidates is supported by its log, not
  an authentication, browser, or extraction failure.
- Candidate discovery and extraction counts are plausible.
- Date decisions include the expected `within_date`, `outside_date`, or
  `unknown_date` behavior.
- Relevance decisions are not rejecting obvious in-scope examples.
- Known-content filtering is not incorrectly skipping a new campaign.
- Content IDs and URLs are populated and deduplicated.
- Comment completeness is honestly marked `complete`, `partial`, `truncated`,
  `unknown`, or `error`.
- Publication time, creator, caption, and engagement completeness are
  acceptable for the intended report.
- Browser/API provenance matches the configured transport.
- Manifest counts match the JSONL line counts and readable combined output.
- Raw platform extensions appear only in `technical_bundle.json` or raw runs,
  not in the readable combined or JSONL analysis artifacts.
- Native-text analysis reports `media_processing_used: false` and explicitly
  records unavailable platform transcripts or subtitles.
- A post is `complete` only for its current input hash and formula version;
  changed comments, text, or metrics mark it `stale`.
- Fact-check verdicts that claim support or contradiction have source URLs;
  unsupported verdicts remain `needs_verification`.
- Every completed analysis has an explicit `publish`, `abstain`, or
  `blocked_by_policy` decision. Mixed, negative, harmful, not-scorable, and
  below-threshold results remain internal and create no draft.
- Positive-only drafts meet the configured score threshold and name both a
  specific strength and one actionable recommendation.
- The original post score independently meets the threshold; conversation value
  is scored separately, weighted at 20% by default, and cannot rescue a weak
  post or change factual integrity.
- Video ratings have a platform transcript or subtitle. Caption-only video
  analysis remains provisional and internal.
- Public comments contain novel, evidence-referenced value, a code-rendered
  one-decimal `/10` rating, natural policy-compliant language, and an explicit AI
  disclosure. They exclude completeness, confidence, rubric state, and raw
  internal scores.
- Publication records remain shadow drafts until confidence, completeness,
  score, evidence-hash, freshness, idempotency, and approval gates pass.
- Required Drive uploads succeeded when Drive is enabled.

If the smoke test fails these checks, fix the campaign spec, login state, or
extractor configuration before increasing limits.

## 7. Run the Full Campaign

After the smoke test is accepted:

```powershell
$Spec = "campaign_specs\client_campaign.json"
python social_browser.py status
python incremental_project.py `
  --campaign-spec $Spec
```

The orchestrator automatically retries the narrowly identified transient
Playwright shared-worker attachment collision once. A login, rate-limit,
transport, or extraction error remains a real failed source and must be fixed.

Limits saved in the campaign specification now control the run. A value of `0`
means unlimited, so use it only when that is intentional.

The orchestrator executes configured sources sequentially, stores raw evidence,
updates the canonical SQLite database, schedules due known-post refreshes,
optionally enriches profiles, synchronizes native-text analysis state,
regenerates reports, and performs configured archival. Shadow analysis never
publishes a comment.

## 8. Final Report-Readiness Gate

Before handing off results, review `compiled\latest\coverage.json` and confirm:

- `configured_sources_pending` is zero for every required platform.
- `configured_sources_failed` is zero, or every failure is documented and
  explicitly accepted.
- Candidate overlap and marginal-new ratios are understood.
- Deduplicated content and comment totals are plausible.
- Outside-window exclusions are expected.
- Partial, truncated, unknown, and error records are quantified.
- Metadata and profile coverage are sufficient for the requested analysis.
- Public profile geography is not presented as audience geography.
- Reach, impressions, traffic, conversions, and audience demographics are not
  claimed from public scraping.
- Drive output and database snapshot status are verified when archival is
  required.

Configured-source completion is the measurable QA target. It is not proof of
complete platform-wide recall.

## 9. Incremental Reruns

Use the same command and same campaign specification for later updates:

```powershell
python incremental_project.py `
  --campaign-spec campaign_specs\client_campaign.json `
  --social-browser
```

The project database skips known content during discovery and schedules bounded
direct refreshes for known posts that are due. Avoid `--rescrape-known` unless a
deliberate full re-extraction is required.

To regenerate reports without collecting platform data or starting the social
browser:

```powershell
python incremental_project.py `
  --campaign-spec campaign_specs\client_campaign.json `
  --compile-only
```

Rerunning after an interruption is safe at the canonical database layer because
content and comments are deduplicated. Review the previous logs and coverage so
an authentication or extractor problem is not repeated unnoticed.

## 10. Stop the Browser When Finished

The social browser may remain running for subsequent campaigns. Stop it only
when authenticated collection is finished:

```powershell
python social_browser.py stop
```

## Operator Sign-Off Checklist

- [ ] New campaign JSON and unique project name created.
- [ ] Scope, platforms, keywords, accounts, dates, and exclusions reviewed.
- [ ] Collection limits are intentionally bounded or intentionally unlimited.
- [ ] Storage is explicitly configured for Drive or local-only operation.
- [ ] Campaign specification validation passed.
- [ ] Social browser is running when browser-backed platforms are selected.
- [ ] Required platform logins were verified.
- [ ] Bounded smoke test completed.
- [ ] Smoke-test coverage and logs were accepted.
- [ ] Full campaign completed.
- [ ] Final pending and failed sources were reviewed.
- [ ] Completeness and metadata limitations were documented.
- [ ] Required archive and database snapshot were verified.
- [ ] Final outputs were taken from the project's canonical `compiled` folder.
