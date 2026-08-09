# Archived LISTEN and Multiplatform Reference

> Do not use commands in this file for TikTok ENGAGE. In particular,
> `incremental_project.py` and `run_scraper.py` are legacy bulk LISTEN
> orchestrators. The active workflow is documented in `README.md` and
> `WORKFLOWS.md`.

# Historical TikTok ENGAGE Notes

This workspace is focused on TikTok ENGAGE: collect evidence for individual
posts, analyze it with the interactive built-in AI, draft and independently
review useful positive responses, store the reviewed responses, and publish
only after explicit user authorization.

Older multiplatform LISTEN/reporting components remain in the repository as
legacy reference. They are not the ENGAGE execution path.

## Workflow Shortcuts

- `LISTEN: <topic>, <post amount>` collects and stores TikTok evidence only. It
  does not analyze, draft, approve, or publish.
- `ENGAGE: <topic>, <post amount>` defaults to `ENGAGE SHADOW`.
- `ENGAGE SHADOW: <topic>, <post amount>` collects, analyzes, drafts, reviews,
  and stores responses without publication.
- `ENGAGE NO-API: <topic>, <post amount>` follows the same gates while using
  Codex/Antigravity interactively instead of an external LLM API.
- `ENGAGE LIVE: <publication id>` requests guarded publication of one stored
  final response after the user has seen and approved its exact text.

`NO-API` and `LIVE` never bypass evidence, review, approval, freshness,
account, duplicate, score, or rate-limit checks. See `AGENTS.md` for the
binding contract and `WORKFLOWS.md` for the complete operating sequence.

## Current Quick Start

Requires Python 3.11 or newer.

```powershell
pip install -r requirements.txt
python -m playwright install chromium
python social_browser.py start
python social_browser.py status
```

Before doing any TikTok work, complete login in the designated social browser
and confirm that the required TikTok account is active. On this workstation
the saved designation is the existing Edge `Profile 7` session. Keep it open
for the workflow. If the browser is unreachable, logged out, or on the wrong
account, stop and fix that state before collection.

Then make a bounded request in chat:

```text
LISTEN | topic="3D printing" | platform=tiktok | posts=50
ENGAGE | topic="photography" | platform=tiktok | posts=10
ENGAGE SHADOW | topic="photography" | platform=tiktok | posts=10
ENGAGE NO-API | topic="cybersecurity" | platform=tiktok | posts=25
```

A request for 50 posts is complete only when 50 unique TikTok post IDs have
evidence-ready records. Duplicates, inaccessible posts, and partial failures
must be replaced by continued discovery. If the workflow cannot reach 50, it
must report `collection_incomplete: X/50` and stop; it must not analyze or
publish an underfilled batch.

Do **not** use `incremental_project.py` or `run_scraper.py` for any ENGAGE
request, including its `LISTEN` collection stage and `ENGAGE NO-API`. Those are
legacy bulk campaign orchestrators. ENGAGE must use verified targeted TikTok
collection that accepts a specific post URL/video ID or enforces the requested
evidence-ready batch count.

## Required ENGAGE Order

```text
social-browser/account preflight
-> collect posts, metrics, captions, transcripts/subtitles, comments/replies
-> verify exact requested evidence-ready count
-> analyze with built-in AI
-> draft with built-in AI
-> run a separate built-in AI critic review
-> store the exact reviewed response in SQLite
-> obtain explicit authorization for that exact response
-> revalidate browser, account, evidence, score, and hashes
-> publish one identified response
-> store the receipt
```

No stage may jump directly from collection to approval or publication. Built-in
AI review approval confirms content quality; it is separate from the user's
authorization to publish. `ENGAGE SHADOW` stops after storage. Before any live
submission, run the browser/account checks again.

## Evidence and Exact Counts

Each counted post must have a canonical ID and URL, creator, caption, current
metrics, transcript/subtitle collection result, comments/replies collection
result, provenance, timestamp, and evidence hash. When TikTok does not provide
transcripts, subtitles, or comments, store an explicit unavailable or zero
result rather than pretending the field was collected.

The requested post count is a collection target, not a publication quota.
After analysis and review, zero through the requested number may be eligible
for positive comments. Low-quality, stale, incomplete, negative, or
low-conversation-value cases are skipped with reasons.

## Deterministic Public Rating

Store separate 0-100 `post_quality_score` and `conversation_value_score`
values. Code combines them under the configured bounded weighting policy into
the canonical 0-100 `analysis_score`; a low post-quality score still blocks
publication. The rating included in an eligible public response comes from
`analysis_score`, converted to `/10` and rounded to the nearest half-point with
decimal round-half-up:

```text
public_rating = round_half_up(analysis_score / 5) / 2
```

Examples: `82 -> 8/10`, `83 -> 8.5/10`, `84 -> 8.5/10`.

Render this canonical rating into the response before AI review and database
storage. Store the final rendered text and its hash. Publication must submit
and verify that exact text, and must reject an unresolved `{PUBLIC_RATING}`
placeholder or score/text mismatch.

## Authenticated Social Browser

The controller records the live WebSocket endpoint in
`comments_data\social_browser\state.json`. Status output reports
authentication cookie names, never values. Do not record cookie values,
authorization headers, or session tokens in evidence, logs, or receipts.

Workers use temporary tabs and disconnect without closing the shared browser.
Use `python social_browser.py stop` only after the workflow has finished; it
disables local debugging and closes only the project browser window, leaving
other Profile 7 windows open.

Each project is stored under `comments_data\project_<project>\`, and its SQLite
database is canonical. Store per-run counters for requested, unique collected,
evidence-ready, analyzed, drafted, reviewed, stored, authorized, published,
skipped, and failed records. A publication receipt must preserve the target,
account, exact submitted text/hash, timestamp, and observed result.

The remaining README sections describe supporting and legacy components. They
do not override the TikTok ENGAGE contract above.

## Public Profile Enrichment

Creators and comment authors are queued during SQLite ingestion. Enable the
API-first enrichment stage after collection and before compilation with:

```powershell
python incremental_project.py `
  --campaign-spec campaign_specs\template.json `
  --profile-enrichment `
  --profile-limit 100 `
  --profile-cache-days 14 `
  --social-browser
```

For an existing project, queue stored identities and run the same stage without
scraping posts again:

```powershell
python post_enrichment.py --project "example_topic" --limit 100
```

Official collectors use `TIKTOK_RESEARCH_ACCESS_TOKEN`, `YOUTUBE_API_KEY`,
`INSTAGRAM_GRAPH_TOKEN` with `INSTAGRAM_BUSINESS_DISCOVERY_ACCOUNT_ID`,
`FACEBOOK_GRAPH_TOKEN`, and `X_BEARER_TOKEN` when available. The dedicated
social browser is the fallback for public profile fields that the current
session can legitimately view. It parses TikTok user-detail responses,
Instagram web-profile JSON, X `UserByScreenName` GraphQL, YouTube channel
metadata/InnerTube data, and Facebook public profile GraphQL or embedded JSON
before falling back to DOM text. Raw response bodies and browser credentials
are not written to profile exports.

Each queued profile has a whole-operation timeout so a changed private web
endpoint cannot block report compilation. Set
`PROFILE_ENRICHMENT_TIMEOUT_SECONDS` to override the 120-second default.
`scratch\probe_profile_web_api.py` can test a few identities from an existing
project database while saving normalized results only.

To navigate explicit profiles without first creating a project database, pass
repeatable targets to the production lookup command:

```powershell
python profile_lookup.py `
  --target "tiktok:@username" `
  --target "instagram:username" `
  --target "x:username" `
  --target "youtube:UC_CHANNEL_ID" `
  --target "facebook:PAGE_OR_PROFILE_ID" `
  --output "comments_data\profile_lookup.json"
```

`--target` also accepts full profile URLs. For larger batches,
`--targets-file targets.json` accepts a JSON array of those strings or objects
with `platform`, `username`/`user_id`, and optional `profile_url` fields. Start
and log into the dedicated browser first with `python social_browser.py start`.
The collector visits each canonical profile route; for Facebook it additionally
visits the public Places Lived and Contact and Basic Info surfaces.

Outputs are written to `compiled\latest\user_profiles.json`,
`user_profiles.csv`, and `profile_coverage.json`. Profile geography records its
evidence basis, such as self-declared location, public business address,
configured channel country, or platform-reported account region, and is never
treated as audience geography. `profile_geography` remains the primary value,
while `profile_geography_evidence` preserves multiple facts such as current
city, hometown, and business address with their source surfaces. Pronouns,
public birthdate text, explicit age text, and visibly labelled profile gender
text can be preserved without normalization.
Non-demographic platform enums such as Facebook Page `NEUTER` are discarded.
The pipeline does not infer gender, ethnicity, or demographics from names,
photos, pronouns, or unrendered internal fields. Private fields are not bypassed.
Audience distributions require first-party analytics and remain aggregate.

## Participant-Owned Profile Export

Use a separate browser directory and local port for each participant. The
participant signs in directly in Edge; passwords and verification codes never
enter the scraper:

```powershell
python social_browser.py start `
  --runtime-dir comments_data\participants\participant_001 `
  --port 9231 `
  --no-open-tabs
```

Export every supported owner profile surface currently available to that
browser session:

```powershell
python participant_profile_export.py export `
  --platform all `
  --cdp-url http://127.0.0.1:9231 `
  --output comments_data\participants\participant_001\owner_profiles `
  --owner-confirmed
```

The command has no target-username argument. It navigates first-party account
and personal-information pages, observes their profile/account responses, and
reads populated editable controls. Instagram also uses the current-account
form endpoints loaded by its own profile editor. Passwords, cookies, CSRF
values, authorization headers, and session tokens are excluded. Terminal
output contains field names and status only.

Exports are protected by Windows DPAPI by default. Use `inspect` to list fields
without values, or explicitly request owner-readable JSON with `--plaintext`:

```powershell
python participant_profile_export.py inspect `
  --input comments_data\participants\participant_001\owner_profiles\instagram.owner-profile.json.dpapi

python participant_profile_export.py export `
  --platform instagram `
  --cdp-url http://127.0.0.1:9231 `
  --output comments_data\participants\participant_001\instagram-owner.json `
  --owner-confirmed `
  --plaintext
```

The owner export preserves explicit demographic and geographic values and
their first-party source surfaces. It does not infer age, gender, ethnicity, or
location, and it cannot add fields that the current account surfaces do not
return.

## Native-Text Analysis And Publication Queue

The analysis workflow uses only text already exposed by a platform: post text,
creator captions/descriptions, platform transcripts/subtitles, comments, and
replies. It does not download media, run speech-to-text, or run image OCR.

Enable shadow-mode analysis in a campaign specification with:

```json
{
  "analysis": {
    "enabled": true,
    "mode": "shadow",
    "provider": "manual",
    "analysis_version": "native-text-v4",
    "scoring": {
      "formula_version": "social-review-v1",
      "post_weight": 80,
      "conversation_weight": 20,
      "max_conversation_adjustment": 10,
      "require_core_content_for_publication": true,
      "video_requires_transcript_or_subtitle": true
    },
    "publication": {
      "mode": "shadow",
      "require_approval": true,
      "positive_only": true,
      "min_public_score": 70,
      "recommendation_only": true,
      "require_strength_and_recommendation": true,
      "require_score": true,
      "require_publication_decision": true,
      "require_novel_value": true,
      "require_evidence_refs": true,
      "require_fresh_evidence": true,
      "max_evidence_age_minutes": 60,
      "max_draft_age_minutes": 60,
      "allow_public_scores": true,
      "public_rating": {
        "enabled": true,
        "required": true,
        "scale": 10,
        "increment": 0.5,
        "placeholder": "{PUBLIC_RATING}"
      },
      "language": {
        "mode": "auto",
        "default": "id",
        "match_creator_tone": true,
        "allow_code_switching": true,
        "neutral_on_low_confidence": true
      },
      "allow_internal_diagnostics": false
    }
  }
}
```

Every post receives a stable evidence-packet hash. A completed analysis is
reused while that hash and the formula version remain unchanged. New comments,
new native transcript/caption text, or changed metrics create a new analysis
record and mark the post `stale`. Only `complete` means the current input has
been analyzed.

`provider: "manual"` or `"codex"` writes pending evidence packets and prompts
to `compiled\latest\analysis_queue.jsonl` without making an external model API
call. Validated results can be imported with:

```powershell
python incremental_project.py `
  --campaign-spec campaign_specs\client.json `
  --compile-only `
  --analysis-import analysis_results.jsonl
```

`provider: "gemini"` uses `GEMINI_API_KEY`, requests JSON output, and can use
Google Search grounding for fact-check sources. A factual verdict without a
valid source URL is downgraded to `needs_verification`. AI dimension subscores
are stored, but code calculates the final weighted score.

Version 4 supplies the default `social-review-v1` rubric. Post quality measures
integrity/evidence 20%, substance/audience value 20%, clarity/craft 15%,
context/completeness 15%, originality/perspective 10%, audience fit 10%, and
responsibility/safety 10%. Conversation value is calculated separately from
relevance, specific useful information, perspective diversity, question
resolution, creator responsiveness, and civility. The default overall score is
80% post quality and 20% conversation value, with comments capped at a ten-point
adjustment. The post score must independently meet the positive threshold, so a
strong discussion cannot rescue a weak or inaccurate original post. Campaigns
may override the dimensions. Explicitly setting `dimensions: []` disables the
formula, leaves the final score `null`, and prevents positive-only publication.

The default publication policy is positive-only. Posts below `70`, posts with
incomplete rubric scores, and mixed, negative, harmful, or not-scorable posts
remain available for internal analysis but create no public draft. Videos also
need a platform transcript or subtitle before a public rating is allowed. This
does not reduce the internal score; it marks the rating provisional and blocks
publication because the core work was not observed. Eligible AI decisions must
identify a concrete strength and one actionable recommendation.

Code converts the internal 0-100 overall score into a public 1-10 rating rounded
to the nearest half point. The AI writes `{PUBLIC_RATING}` and code replaces it,
so the model cannot invent or round the rating. Raw scores, completeness,
confidence, and rubric diagnostics remain private. Language defaults to `auto`:
the AI follows the post and audience language, can use natural code-switching,
falls back to neutral Indonesian when uncertain, and must retain an explicit
`AI` disclosure. An abstention is a successful analysis outcome.

Collection, analysis, and publication are separate stages coordinated by the
same pipeline. Collection remains canonical and succeeds independently of AI or
publication outcomes. The pipeline only creates publication drafts. Drafts require configured
confidence and completeness, a qualifying internal formula score, approval, an
unchanged evidence hash, and unexpired evidence and analysis timestamps. They
are deduplicated by post and analysis version. The queue rechecks current
SQLite evidence before insertion, and a live adapter rechecks it again before
typing. No public-write adapter is enabled by the analysis or publication mode
flags alone.

To study one legitimate manual comment from the designated authenticated
browser without replaying it or storing credentials:

```powershell
python comment_publication_capture.py `
  --platform youtube `
  --url "https://www.youtube.com/watch?v=EXAMPLE" `
  --project example_topic `
  --database "comments_data\project_example_topic\state\scrape_state.sqlite"
```

The capture utility records candidate endpoint shapes, redacted request-body
templates, response status, and dynamic signature field names. Cookies,
authorization, session, CSRF, and signature values are redacted. It never
submits or replays a request.

## Google Drive Archive

Active worker files and the live SQLite database stay on the local disk. When a
campaign enables `storage.provider: "google-drive"`, the orchestrator uses the
Drive v3 API to create this app-owned hierarchy:

```text
Social Listening Projects/
  project_<project>/
    raw_runs/
    compiled/
    logs/
    state/
    manifests/
```

Uploads are resumable and recorded in the project's `archive_uploads` SQLite
ledger. Every file is checked against Drive's size and MD5 response. A raw run
is eligible for local deletion only after all files and its final manifest are
verified. By default, raw runs are archived immediately after each source. Set
`storage.raw_run_archive_timing: "final"` to scrape and ingest all sources
first, then reconcile completed raw runs at the final archive step. Compiled
files and logs are updated in place, and the database is uploaded from
SQLite's backup API as `scrape_state_latest.sqlite`.

Google Drive for desktop and the Drive API use separate authorization. The
`I:\My Drive` setting verifies that the intended desktop mount is present, but
the API still needs one Desktop OAuth client and one consent:

1. In Google Cloud, enable **Google Drive API** for a project.
2. Configure its OAuth consent screen. Testing mode is enough for initial
   validation; add the archive account as a test user.
3. Create an OAuth client with application type **Desktop app**.
4. Download the JSON to `.google_drive\client_secret.json`.
5. Authorize and initialize the destination:

```powershell
python google_drive_setup.py `
  --account "drive-account@example.com" `
  --mount-path "I:\My Drive" `
  --project "example_topic"
```

The refresh token is stored outside the repository by default at
`%LOCALAPPDATA%\KitaCoLab\SocialListening\google_drive_token.json`. The OAuth
scope is `drive.file`, so the scraper can manage files it creates without
receiving blanket access to unrelated Drive content. After setup, normal
campaign commands are deliberately noninteractive and require no browser. An
external OAuth app left in Testing receives a refresh token that expires after
seven days; for unattended operation, move it to In production (and complete
any verification Google requires), or use an internal/trusted Workspace app.
Machine settings may also be
provided with `GOOGLE_DRIVE_CLIENT_SECRET_FILE`, `GOOGLE_DRIVE_TOKEN_FILE`,
`GOOGLE_DRIVE_ACCOUNT_HINT`, and `GOOGLE_DRIVE_MOUNT_PATH`.

Publication windows are applied before comment extraction when discovery
exposes a parseable date. Out-of-window candidates remain in the provenance
ledger with an `outside_date` status. Candidates whose date is not exposed are
retained as `unknown_date` to avoid silently reducing recall. Known posts due
for refresh are sent directly back to platform extractors, so comment updates
do not depend on search rediscovering the post.

Incremental runs skip already-known content while scheduling a bounded refresh
of recent known posts. Defaults are 20 refreshes per platform after six hours;
override them with `--refresh-known-limit` and
`--refresh-known-after-hours`. Set `--refresh-known-limit 0` to collect only
newly discovered posts.

YouTube transcripts use `youtube-transcript-api` by default. The scraper spaces
transcript starts by 0.75 seconds, retries transient failures once, and opens a
15-minute transcript-only circuit after rate limiting or request blocking;
video metadata and comments continue during that cooldown. Tune these with
`--youtube-transcript-delay`, `--youtube-transcript-retries`, and
`--youtube-transcript-cooldown`. Use `--youtube-transcript-method auto` only for
diagnostics that should try the older InnerTube and signed timedtext fallbacks.

TikTok transcripts use subtitle manifests returned to TikTok's web player.
The collector prefers Indonesian and then English, downloads the selected
WebVTT track, and preserves both plain text and timestamped cue segments.
Original captions, automatic speech recognition, and machine translations are
identified in `subtitle_selected_track`; expiring signed URLs are not archived.
Posts without a caption track receive `transcript_status: unavailable`, while
metadata, download, and parsing failures have separate statuses and never stop
comment collection. Use `--tiktok-transcript-langs`,
`--tiktok-transcript-timeout`, and `--tiktok-transcript-retries` to tune the
collector, or `--no-tiktok-transcripts` to disable it explicitly.

## X Collection

X defaults to `browser-api`, a zero-official-API-cost transport that uses an
authenticated browser only to request and paginate X's web GraphQL timelines.
Post and reply data come from `SearchTimeline`, `UserTweets`, and `TweetDetail`
JSON responses, not page text or DOM selectors. Create a reusable login state
once:

```powershell
python scratch\save_x_storage_state.py

python incremental_project.py `
  --project "x_test" `
  --keywords "example topic" `
  --platform x `
  --videos 5 `
  --comments 10 `
  --x-storage-state "comments_data\x_storage_state.json"
```

Use `@handle` or an X profile URL for account collection; ordinary text remains
a keyword search. Raw exports record GraphQL operation provenance and explicitly
report zero official API reads. The stateful runner still deduplicates known
post IDs, although the web search itself has no supported `since_id` parameter.
This route depends on X's private web interface and a legitimate user session,
so it is more brittle than an official API and cannot promise hidden, deleted,
or ranking-suppressed replies. Keep request pacing conservative, follow X's
terms, and treat `x_storage_state.json` as a secret.

The paid official adapter remains available with `--x-extraction-mode official`.
It uses API v2 paginated search and `conversation_id:<post_id> is:reply`, rebuilds
direct and nested reply trees, records read/cost accounting, and supports
`--x-search-mode recent` or eligible full-archive `all` access. Configure its
bearer token through `X_BEARER_TOKEN` or `--x-bearer-token-file`; never store
bearer tokens in campaign JSON or commit them to the repository.

A comprehensive tool for scraping comments from TikTok videos and compiling them into various formats for analysis.

## Features

- Automatically scrapes comments from TikTok videos using hybrid methods
- Uses direct API access when possible for faster and more efficient scraping
- Falls back to browser-based scraping when API access is limited
- Records UI elements for reliable scraping across sessions
- Navigates between videos automatically
- Saves comments to JSON files
- Creates diagnostic logs for troubleshooting
- Can scrape all videos from a user profile with no limitations

## Scraper Requirements

- Python 3.11+
- Microsoft Edge browser installed
- Playwright
- An existing Edge profile (for TikTok authentication)

## Scraper Installation

1. Clone this repository:
```
git clone <repository-url>
cd tiktok-scraper
```

2. Install the required dependencies:
```
pip install playwright requests aiohttp
python -m playwright install chromium
```

## Scraper Usage

### Basic Usage

Run the scraper with default settings (10 videos):

```
python run_scraper.py
```

### Recording Mode

The first time you use the scraper, you'll need to record the comment button location:

```
python run_scraper.py --record
```

Follow the on-screen instructions to:
1. Navigate to a TikTok video with comments
2. Click on the comment button when prompted
3. Wait for confirmation

### Command-Line Options

The scraper supports several command-line options:

```
python run_scraper.py --help
```

Available options:
- `--url URL` - URL of TikTok profile or video to scrape
- `--videos N` - Maximum number of videos to scrape (default: 10)
- `--percentage N` - Percentage of comments to scrape per video (default: 50)
- `--no-api` - Disable API-based scraping, use only browser-based method
- `--record` - Run in recording mode to capture UI elements
- `--debug` - Enable debug mode with more verbose logging

### Scrape a Specific Profile

Scrape videos from a specific TikTok profile:

```
python run_scraper.py --url https://www.tiktok.com/@username --videos 50
```

This will:
1. Navigate to the specified profile
2. Find all regular videos (excluding playlists and live streams)
3. Scrape comments from each video (up to 50 videos)
4. Save all data to the comments_data folder

## Scraping Methods

The scraper now uses a hybrid approach for maximum efficiency and reliability:

### API-Based Scraping (New)

The scraper first attempts to use TikTok's official API endpoints to retrieve comments:

- Extracts security tokens from your browser session
- Makes direct API calls to TikTok's comment endpoints
- Refreshes tokens periodically to maintain session validity
- Much faster and more efficient than browser-based scraping

### Browser-Based Scraping (Fallback)

If API-based scraping fails, the scraper automatically falls back to browser-based scraping:

- Uses network interception to capture API responses as they happen
- Scrolls the comment section to trigger API calls
- Processes the captured responses to extract comments
- More reliable but slower than direct API access

You can disable API-based scraping with the `--no-api` flag if you experience issues.

## Comment Gathering

The scraper is optimized to capture approximately 50% of all available comments on each video by default. You can adjust this percentage using the `--percentage` option.

When using API-based scraping, the scraper can often retrieve close to 100% of comments much more quickly than with browser-based methods.

Note that for videos with thousands of comments, each video may still take some time to process, especially if API-based scraping is disabled.

## Output

All scraped data is saved in the `comments_data` folder, organized by session:

- `comments_data/session_YYYYMMDD_HHMMSS/comments/` - Scraped comments in JSON format
- `comments_data/session_YYYYMMDD_HHMMSS/logs/` - Diagnostic logs and recorded element information

## Customization

You may need to modify the Edge profile path in the `tiktok_scraper/main.py` file to match your system:

```python
profile_path = r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data\Profile 2"
```

## Troubleshooting

- If API scraping fails, try running with `--no-api` to use browser-based scraping only
- If the scraper fails to find the comment button, run in `--record` mode to re-record it
- Check the logs folder for detailed error information
- Make sure you're logged into TikTok in the Edge profile you're using
- If navigation between videos fails, try finding a popular video feed first

## Technical Details

### Security Token Management

The API integration extracts several important security tokens from your TikTok session:

- `msToken`: Main security token used for most API requests
- `verifyFp`/`s_v_web_id`: Device fingerprint verification
- `csrf_token`: Cross-site request forgery protection token
- `ttwid`: TikTok web ID token

These tokens are refreshed automatically during scraping to ensure session validity.

### API Endpoints

The scraper monitors and interacts with several TikTok API endpoints:

- `/api/comment/list/`: Main comment list API
- `/api/comment/detail/`: Comment details API
- `/api/comment/reply/list/`: Reply comments API
- `/api/item/detail/`: Video details API
- `/api/user/detail/`: User details API

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

Use this tool responsibly and in accordance with TikTok's terms of service. This project is for educational purposes only.

# TikTok Comments Compiler

The TikTok Comments Compiler is an enhanced tool that processes the JSON files created by the scraper into formats that are easier to analyze, including PDF reports optimized for LLM processing.

## Compiler Features

- **User-Friendly GUI Interface**: Easy-to-navigate interface with multiple processing options
- **Batch Processing**: Process multiple session folders in parallel to save time
- **Multi-Format Output**: Create JSON, PDF, and special high-engagement PDFs
- **LLM-Optimized Output**: PDFs are structured with clear markers and tables for easy LLM analysis
- **Sentiment Analysis**: The high-engagement PDFs include sentiment analysis of comments
- **Hashtag Detection**: Automatically detects and analyzes hashtags in comments
- **Performance Optimization**: Efficiently processes thousands of comments
- **Detailed Documentation**: Built-in help and instruction system

## Compiler Requirements

- Python 3.11+
- ReportLab library (optional, for PDF generation)

To install ReportLab:
```
pip install reportlab
```

## Compiler Usage

### GUI Mode

Simply run the compiler to open the graphical interface:

```
python comment_compiler.py
```

Or use the provided batch file (on Windows):

```
run_compiler.bat
```

### Command Line Options

The compiler also supports command-line operation:

```
# Process a specific folder
python comment_compiler.py /path/to/session_folder

# Run in batch processing mode
python comment_compiler.py batch

# Run a test on sample data
python comment_compiler.py test
```

### Output Files

For each processed session, the compiler generates:

1. **Complete JSON** (`session_name_complete_comments.json`)
   - Contains all original data from the comment files

2. **Minimized JSON** (`session_name_minimized_comments.json`)
   - Contains only essential information for smaller file size

3. **Full PDF Report** (`session_name_comments.pdf`)
   - All comments in a structured format with clear section markers
   - Organized in a table layout for easier reading
   - Contains machine-readable markers for LLM analysis

4. **High-Engagement PDF** (`session_name_high_engagement_comments.pdf`)
   - Contains only popular comments (high like counts)
   - Includes sentiment analysis (positive/negative/neutral)
   - Analyzes hashtags used in comments
   - Ideal for LLM processing as it's much more compact

## Advanced Features

### Sentiment Analysis

The high-engagement PDF automatically categorizes comments as positive, negative, or neutral based on keyword analysis and provides statistics on sentiment distribution.

### Hashtag Analysis

The compiler detects hashtags in comments and provides analytics on the most frequently used tags.

### Batch Processing

Process multiple session folders in parallel with a configurable number of worker threads. This is especially useful when dealing with large datasets from multiple scraping sessions.

## Tips for LLM Analysis

1. For very large datasets, use the high-engagement PDF which filters to just the most popular comments.
2. The PDFs include special markers (`VIDEO_START` and `VIDEO_END`) that make it easier for LLMs to navigate the document.
3. Comments are presented in table format for better structure recognition.
4. The sentiment analysis provides a quick overview of audience reaction.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

Use this tool responsibly and in accordance with TikTok's terms of service. This project is for educational purposes only.
