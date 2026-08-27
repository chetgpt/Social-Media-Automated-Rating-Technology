# Workspace reorganization — 2026-08-26

## Purpose

This cleanup reduces root-level clutter while preserving every canonical
TikTok workflow, guarded MUSIC AUDIT run, and workspace-global registry at its
existing path. It is an organizational change only; it does not change MUSIC
AUDIT collection scope, evidence schemas, browser behavior, or publication
authority.

## Safety boundary

- No file was deleted.
- Existing `comments_data/project_music_audit_*` directories were not renamed,
  migrated, or moved.
- Existing guarded handoff, ledger, review, evidence, and SQLite paths were not
  rewritten.
- The shared `comments_data/tiktok_master/state/tiktok_master.sqlite` registry
  remains in place.
- Canonical entry points, `.agents`, `tiktok_scraper`, and `sonic_audit` remain
  at their required paths.
- Generated material and caches were moved to ignored, reversible storage under
  `local_artifacts/`.

## Resulting top-level roles

| Path | Role |
| --- | --- |
| `.agents/` | Canonical agent skills and guarded MUSIC AUDIT operator |
| `comments_data/` | Immutable-location workflow outputs and shared registry |
| `config/` | Reusable campaign specifications and topic profiles |
| `docs/` | Active contracts, legacy guidance, and cleanup records |
| `legacy/` | Retained one-off tools, experiments, and superseded implementations |
| `local_artifacts/` | Ignored generated reports, queues, caches, diagnostics, and scratch data |
| `tests/` | Executable tests grouped by functional area |
| `tiktok_scraper/` | Active workflow implementation |
| `tools/` | Maintained utilities and historical export tools |

## Moves made

- `campaign_specs/` → `config/campaign_specs/`
- `topic_profiles/` → `config/topic_profiles/`
- Active contract documents → `docs/contracts/`
- Retired instructions → `docs/legacy/`
- Root `old_code/`, browser recorders, JSON compiler, and unreferenced one-off
  scripts → categorized folders under `legacy/`
- Historical export scripts → `tools/historical_exports/`
- Root-generated queues, evidence exports, databases, reports, browser
  diagnostics, fixtures, temporary folders, logs, and scratch data →
  categorized folders under `local_artifacts/`
- Test modules → `tests/workflows/`, `tests/publication/`,
  `tests/integrations/`, `tests/unit/`, and `tests/sonic/`
- Test-generated MUSIC AUDIT result files → `local_artifacts/reports/test_results/`
- The Wardah test fixture export →
  `local_artifacts/fixtures_and_samples/wardahofficial_audit/`
- A 51-file creator-audit analysis batch that appeared at the root after the
  paused snapshot →
  `local_artifacts/sessions/2026-08-26_concurrent_creator_audits/` as one
  preserved, flat historical bundle.

## Compatibility updates

Documentation and tests were updated to reference the new non-runtime paths.
`pytest.ini` excludes `legacy/` and `local_artifacts/` from collection. The
canonical guarded MUSIC AUDIT layout remains exactly the versioned layout
defined in `AGENTS.md`.

## Validation record

Validation completed after all moves:

- Root reduced to 55 files and 14 directories; no cache directory remains at
  the root.
- Pytest discovery found exactly the 973-test baseline.
- Full suite: **973 passed**, one pre-existing deprecation warning, in 145.35
  seconds.
- All nine canonical command surfaces returned successful `--help` results:
  `engage_tiktok.py`, `quick_audit_tiktok.py`, `music_backfill_tiktok.py`,
  `sonic_audit_tiktok.py`, `social_music_audit.py`, `linkedin_workflow.py`,
  `tiktok_one_top_content.py`, `social_browser.py`, and the guarded MUSIC AUDIT
  operator.
- The historical handoff checkpoint remained unchanged: 17 layout-v1 and 10
  layout-v2 handoffs; 9 v1 plus all 10 v2 are structurally valid. The same 8
  legacy-v1 handoffs remain invalid at their original paths, with no new
  invalid handoff introduced by this cleanup.
- Final guarded smoke: project
  `music_audit_test_new_topic_music_1p_20260826_203723_733482`, run
  `engage_559bb684508046ca`, `collection_complete`, evidence-ready `1/1`,
  executor compliance `PASS`, and validated layout-v2 export. Profile 7 was
  reachable, authenticated, and verified in `existing_profile_attach` mode.
  AI actions and outbound actions were both empty.

The final smoke review is stored at
`comments_data/project_music_audit_test_new_topic_music_1p_20260826_203723_733482/test_1p_music_260826_afcb02ee_review.md`.
