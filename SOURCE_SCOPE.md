# Source and Backup Scope

The private GitHub repository stores source code, tests, documentation, and
reusable configuration. It is not a backup of harvested TikTok evidence or
authenticated workstation state.

## Active workflow source

`AGENTS.md` and `WORKFLOWS.md` define the binding TikTok-only LISTEN, AUDIT,
ENGAGE, publication, and comment-showcase paths. The active implementation is
centered on `engage_tiktok.py`, `social_browser.py`,
`tiktok_master_database.py`, the guarded publication/showcase modules, and
their tests.

## Supporting and legacy source

Some source is retained because it provides reusable profile, enrichment,
export, storage, and test support. Older multi-platform collectors and metrics
modules may also be retained for reference. Their presence does not authorize
their use for LISTEN, AUDIT, or ENGAGE.

In particular, `incremental_project.py`, `run_scraper.py`,
`metrics_tiktok_scraper/`, and older platform scrapers are legacy/reference
paths. They must never replace the targeted workflow commands required by
`AGENTS.md`.

## Intentionally excluded from Git

- `comments_data/`, SQLite databases, evidence packets, comments, screenshots,
  generated reports, queues, receipts, and browser diagnostics;
- `.env` files, API/OAuth tokens, cookies, authenticated browser state, and
  local media-hosting configuration;
- caches, bytecode, logs, temporary directories, and generated payloads;
- one-off scripts that fabricate analysis/review state, mutate workflow hashes
  or approvals directly, impersonate human authorization, publish without the
  guarded adapters, or launch a substitute browser profile.

Those exclusions are deliberate. Runtime data that needs disaster recovery
must be backed up separately using SQLite-consistent and encrypted storage,
not committed to Git.
