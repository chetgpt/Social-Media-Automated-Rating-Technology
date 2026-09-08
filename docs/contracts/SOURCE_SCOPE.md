# Source and Backup Scope

The private GitHub repository stores source code, tests, documentation, and
reusable configuration. It is not a backup of harvested TikTok evidence or
authenticated workstation state.

## Active workflow source

The separate recency-first POSTS DISCOVERY mode consists of
`posts_discovery.py`, its publication-window support in the guarded topic
collector/export path, `.agents/skills/posts-discovery/SKILL.md`,
`docs/contracts/POSTS_DISCOVERY.md`, and focused tests. These are versionable
source. The coordinator freezes topic, positive post count, and publication
window, then delegates to one exact guarded MUSIC AUDIT topic child. Full child
evidence stays in the canonical project directory with normal master lineage.
Its manifest binds exact child paths; parent reporting reads validated evidence
without modifying child or master state. Publication-window enforcement does
not imply exhaustive TikTok coverage or semantic verification of every topic
match. Generated manifests, posts SQLite/JSONL, and review JSON/Markdown under
`comments_data/posts_discovery_runs/` are runtime data, not Git source.

The legacy `music_discovery_search.py`, `music_discovery.py`,
`music_discovery_sources.py`, `music_discovery_corpus.py`,
`music_discovery_classification.py`, legacy skill/contract, and focused tests
remain versionable source for explicit legacy research continuation. Existing
briefs, live-search manifests, corpus generations, review checkpoints/packets,
dossiers, SQLite state, and shortlists under
`comments_data/music_discovery_runs/` remain runtime data at their old paths.
They are not migrated or retrospectively certified as POSTS DISCOVERY output.

The explicit multi-topic MUSIC AUDIT coordinator source consists of
`music_audit_topics.py`, its focused tests,
`docs/contracts/MULTI_TOPIC_MUSIC_AUDIT.md`, and the corresponding workspace and
skill instructions. It freezes ordered normalized topics, one
TOTAL/EACH/CUSTOM quota plan, positive fixed child quotas, plan hash, and exact
guarded child bindings. TOTAL uses deterministic floor/remainder allocation in
input order and rejects a total smaller than the number of topics. Every child
is an ordinary sequential exact-query `workflow=listen`, `new_only` MUSIC AUDIT
run with its own canonical project database/artifacts and the shared master
lineage. Global deduplication gives overlap to the earliest child; quotas are
never borrowed or redistributed. Parent `status` and `validate` are offline,
and continuation may address only the saved current child handoff. Generated
manifests, state, reviews, and child-link records beneath
`comments_data/music_audit_topic_runs/` are runtime data, not Git source.

`AGENTS.md` and `WORKFLOWS.md` define the binding TikTok-only PULSE,
MUSIC AUDIT (implemented as `workflow=listen`), MUSIC AUDIT BACKFILL, isolated
SONIC AUDIT, AUDIT, ENGAGE, publication, and comment-showcase paths. The active implementation is
centered on `quick_audit_tiktok.py`, `engage_tiktok.py`,
`music_backfill_tiktok.py`, `sonic_audit_tiktok.py`, the `sonic_audit/`
package including `sonic_audit/corpus.py` and `sonic_audit/evaluation.py`,
`sonic_audio_transport.py`,
`social_browser.py`,
`tiktok_master_database.py`, `tiktok_scraper/music_enrichment.py`,
`tiktok_scraper/tt2dsp_resolution.py`, the guarded
publication/showcase modules, and their tests. Canonical MUSIC AUDIT supports
topic, exact-creator, and exact-URL collection through the LISTEN engine. TikTok
music declarations and eligible Apple exact-ID resolution are collected before
evidence hashing. MusicBrainz is retired; its compatibility schema preserves
historical results and produces terminal retirement outcomes without requests
for preserved runs. See `MUSICBRAINZ_RETIREMENT.md`. This remains an evidence
collection path with no AI analysis or publication.
The source-only cross-platform MUSIC AUDIT substrate consists of
`social_music_audit.py`, `tiktok_scraper/social_music_contract.py`,
`tiktok_scraper/social_music_projection.py`,
`tiktok_scraper/social_music_state.py`, and their focused tests. It may import
and export already collected, normalized Instagram, Facebook, X, and YouTube
metadata under a closed collection-only contract. It does not authorize the
legacy multiplatform runner as a live canonical adapter, acquire media, call an
AI model, or create engagement/publication state.

The separate active LinkedIn source consists of `linkedin_workflow.py`,
`tiktok_scraper/linkedin_api.py`, `tiktok_scraper/linkedin_state.py`,
`docs/contracts/LINKEDIN_WORKFLOW.md`, and the focused `tests/integrations/test_linkedin_*.py` tests. It is
an official-API, organization-Page-only collector with isolated retention-aware
SQLite state. Its `listen` and `engage` values both stop at collection. It
accepts only an administered organization URN or one exact authorized
organization-post URN; it does not authorize topic/arbitrary-member collection,
`ALL`, browser scraping, export, AI, drafting, approval, or publication. It
never reads or writes the TikTok master/workflow databases or the
platform-neutral social-music database. Runtime authorization comes only from
`LINKEDIN_ACCESS_TOKEN`, which remains outside source and durable state.

The separate active TikTok One link-discovery source consists of
`tiktok_one_top_content.py`, `tiktok_scraper/top_content_browser.py`,
`tiktok_scraper/top_content_state.py`, `docs/contracts/TIKTOK_ONE_TOP_CONTENT.md`, and their
focused tests. It attaches only to Profile 7 and stores query-free canonical
TikTok video links plus bounded rank/filter provenance in
`comments_data/tiktok_one/`. It does not back up captions, comments, media,
HTML, raw responses, credentials, or browser state. Its sequential bridge
delegates eligible links to canonical one-URL MUSIC AUDIT children; discovery
state never becomes canonical evidence or joins the TikTok master database.

MUSIC AUDIT BACKFILL is the reusable append-only maintenance path for known
posts with missing, older-schema, or selected retryable music evidence. It
binds each new music observation to an immutable older evidence snapshot/hash
and never refreshes that snapshot's metrics, transcripts, comments, registry
`last_seen`, global-new status, or publication history. Direct HTML is primary;
only a failed frozen exact URL/post-ID target may inventory its affected exact
owner once, consume the matching frozen ID/owner/media type, and ignore all
unrelated rows. Topic scope has no profile fallback. Terminal profile absence
becomes `unavailable`, while an unproven absence at a nonterminal frontier
leaves the run incomplete/resumable. This narrow path adds no command and
collects no metrics, comments, transcripts/subtitles, audio, or AI output.
SONIC AUDIT is the separate permission-gated acoustic research path for 1-60
public video posts in a frozen exact-creator set already known to the master
registry.
It reads that registry without writing it, uses Profile 7 for bounded transient
media acquisition only after explicit per-run authorization, deletes all raw
media/audio/intermediates on success or failure, and stores only versioned
derived hashes, features, checkpoints, evaluation, optional offline statistical
validation, and exports under `comments_data/sonic_audit_runs/`. The
`validate --run-id` stage reads only a completed immutable run, binds its
manifest/report/ordered-feature-set hashes, and writes only the run-local
`statistical_validation.json`; it performs no browser, media/audio, AI, or
master-registry access and requires no new transient authorization. Its wrapper
schema is `tiktok-sonic-audit-statistical-validation-v1`, with nested
`sonic-statistical-validation-v1`. `status` and `export` remain read-only, and
export includes this artifact when present. Offline `validate-suite` combines
at least two ordered complete, nonoverlapping same-creator/master/feature-
contract runs and writes only a no-clobber
`tiktok-sonic-audit-statistical-validation-suite-v1` artifact. A new
labelled-first extension run may freeze complete same-creator/master prior-run
exclusion hashes with `--exclude-run-id`; it remains capped at 60 posts and
requires new authorization. This exception does not authorize audio
download or acoustic recognition in MUSIC AUDIT, backfill, AUDIT, PULSE, or
ENGAGE.

The same runner's `plan-corpus` command is a non-run, offline exception to the
executable creator-only scope. With global `--master-database` and
`--output-root` preceding the subcommand, it accepts exactly one repeatable
scope (`--creator` or exact `--post-id`), creator-only repeatable
`--exclude-run-id`, and required `--min-repeated-groups`,
`--min-positive-pairs`, `--max-posts`, `--max-posts-per-reference`, and
`--file`. It reads only hash-verified, completed, current v3 music-backfill
observations with exact resolved Apple `tt2dsp` labels. It creates a no-clobber
`tiktok-sonic-audit-corpus-plan-v1` artifact with bound scope, evidence and
exclusion hashes, repeated reference groups, and deterministic single-creator
batches of at most 60 posts. It performs no browser, provider, media/audio, AI,
or master write, creates no SONIC run, and grants no audio authorization.

`run-plan-batch` is the guarded execution bridge for exactly one plan batch.
It requires fresh explicit authorization for that batch and validates all plan,
scope, candidate, group, batch, and set hashes plus every current master/
evidence/music/Apple binding before browser access or run creation. It freezes
one creator and at most 60 ordered unique videos without substitution,
preserves typed Apple identity and plan-selection provenance, and freezes an
expected account when supplied. A duplicate plan-hash/batch-ID run is rejected
in favor of `resume`; the normal status, validation, suite, and export source
paths remain applicable. Its media is bounded and transient with cleanup, its
master access is read-only, it invokes no built-in semantic AI, and its result
remains exploratory. The separately gated Mirelo symbolic diagnostic below is
the only optional learned-provider branch. The plan itself still grants no
execution authority.

The standalone Audio Archive source consists of `audio_archive_tiktok.py`, its
focused support modules and tests, `run_audio_archive_batch.py`, and
`docs/contracts/AUDIO_ARCHIVE.md`. It reads evidence-ready rows query-only from
any compatible current, incomplete, copied, moved, restored, or legacy project
database. It requires no master binding, terminal status, original path,
rights/storage/TTL form, or separate authorization statement. It freezes all
usable evidence-ready public videos or a selected subset and never discovers,
refreshes, or substitutes posts. `run` and `resume` use Profile 7 for one
acquisition per item,
then locally create one normalized M4A AAC-LC 192 kbit/s 44.1 kHz file and one
MP3 192 kbit/s 44.1 kHz file. Each format has its own size and SHA-256 binding;
the pair commits as one logical checkpoint and no half-pair counts. Handled
failure rolls back both names; same-run resume removes exact manifest-owned
residue after a hard interruption. Temporary media and source video are
deleted after success and handled failure. Status, validation, and batch
`--dry-run` are offline. The workflow has no AI, provider upload, source write,
engagement, or publication capability.

Optional Mirelo Audio-to-MIDI remains inside the isolated SONIC source scope.
It can be selected only when creating an ad hoc `run` or exact
`run-plan-batch` with `--mirelo-audio-to-midi`, the ordinary
`--authorize-transient-audio`, a separate exact-run rights/upload attestation
through `--authorize-mirelo-upload`, and a positive frozen
`--mirelo-max-credits N` ceiling. Its secret is runtime-only
`MIRELO_API_KEY`; source, fixtures, plans, manifests, logs, databases, and
exports must never contain it. Preflight must keep provider work within the
frozen credit budget and exact candidate set. The provider may retain assets
for up to 24 hours, so local cleanup is not represented as remote deletion.

Only sanitized, non-reconstructable, hash-bound aggregate symbolic summaries
and safe provider/config/credit/timing provenance may become run artifacts.
Uploaded/decoded audio, MIDI, MusicXML, raw/structured notes, instrument-track
payloads, raw responses, and provider job/result/download URLs remain transient
and outside both Git and durable run storage. Mirelo output is a separate
probabilistic diagnostic: it never changes the primary `recording_score` or
supports identity, genre, mood, lyrics, ownership, quality, or causal claims.
Resume uses the frozen provider/upload/budget contract; `status` and `export`
stay offline. Provider adapter tests are checked-in only as mocked transports
and synthetic fixtures, never live uploads or credit-bearing calls. The source
contract follows Mirelo's [API docs](https://mirelo.ai/api-docs#audio-to-midi),
[model page](https://mirelo.ai/models/audio-to-midi), and
[terms](https://mirelo.ai/terms).

## Supporting and legacy source

Some source is retained because it provides reusable profile, enrichment,
export, storage, and test support. Older multi-platform collectors and metrics
modules may also be retained for reference. Their presence does not authorize
their use for PULSE, LISTEN, AUDIT, or ENGAGE.

In particular, `incremental_project.py`, `run_scraper.py`,
`metrics_tiktok_scraper/`, and older platform scrapers are legacy/reference
paths. They must never replace the targeted MUSIC AUDIT or other workflow commands required by
`AGENTS.md`.

`legacy/one_off_state_mutators/enrich_music_data.py` is also a legacy standalone experiment. It must never be
run against a canonical exported AI queue or used to mutate hash-bound
evidence. Active music enrichment is collector-owned and uses
`tiktok_scraper/music_enrichment.py` plus the bounded tt2dsp resolver through
`engage_tiktok.py`.
Older music evidence must be upgraded through `music_backfill_tiktok.py`, not
through this legacy experiment or a raw database rewrite.

## Intentionally excluded from Git

- `comments_data/`, SQLite databases, evidence packets, comments, screenshots,
  generated reports, queues, receipts, and browser diagnostics, including the
  isolated LinkedIn collection database and its 48-hour comment/180-day post
  retention payloads and URN tombstones;
- `.env` files, API/OAuth tokens, cookies, authenticated browser state, and
  local media-hosting configuration;
- caches, bytecode, logs, temporary directories, and generated payloads;
- runtime catalog lookup caches, provider responses, music backfill runs and
  exports, and music-enrichment evidence/results (source adapters and
  deterministic tests remain in Git);
- SONIC AUDIT corpus plans, run manifests, checkpoints, derived fingerprints/
  features, evaluations, `statistical_validation.json`, exports, temporary
  downloads/decodes, raw video/audio/PCM, intermediate spectrograms, Mirelo
  input/output audio, MIDI, MusicXML, raw notes/instrument tracks, provider
  payloads/URLs, API keys, and acoustic-model caches (runner/source adapters
  and deterministic or provider-mocked fixtures/tests remain in Git);
- Audio Archive manifests, state, records, reviews, batch reports, normalized
  M4A and MP3 files, source downloads, partial files, transcoder scratch data,
  and archive contents under `comments_data/audio_archive_runs/`
  (runner/source and deterministic tests remain in Git);
- multi-topic MUSIC AUDIT parent manifests, state, reviews, validations, and
  child-link records under `comments_data/music_audit_topic_runs/` (the
  coordinator, contract, and focused deterministic tests remain in Git; child
  evidence stays in its canonical ignored project directory);
- one-off scripts that fabricate analysis/review state, mutate workflow hashes
  or approvals directly, impersonate human authorization, publish without the
  guarded adapters, or launch a substitute browser profile.

Those exclusions are deliberate. Runtime data that needs disaster recovery
must be backed up separately using SQLite-consistent and encrypted storage,
not committed to Git.
