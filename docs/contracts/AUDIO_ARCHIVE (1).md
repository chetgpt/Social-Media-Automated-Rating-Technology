# Audio Archive Workflow

## Purpose and isolation

`audio_archive_tiktok.py` is a standalone, permission-gated workflow for
retaining normalized audio from an exact, already completed TikTok workflow
run. It may use evidence-ready posts from canonical LISTEN (including MUSIC
AUDIT), AUDIT, or ENGAGE state. It does not add audio acquisition to those
workflows, reopen them, or change their evidence, analysis, approval, or
publication contracts.

The workflow has two storage modes:

| Mode | Permitted basis | Maximum retention |
| --- | --- | --- |
| `permanent` | The named authorizing human attests that the audio is `owned`, `licensed`, or covered by `written-permission` | Indefinite; the operator remains responsible for rights and deletion |
| `public-research` | Internal research from a public TikTok post, using the matching `public-research` basis | A required integer TTL from 1 through 30 days |

The mode is frozen at run creation and cannot change on resume. Public
availability is not evidence of ownership or a license and cannot be the basis
for permanent retention.

## Closed source scope

Every archive run accepts exactly one local source identity: the path of one
canonical per-project workflow SQLite database and one exact completed run ID
inside it. The source must be LISTEN/MUSIC AUDIT, AUDIT, or ENGAGE, and its
collection stage must already be complete.

PULSE/QUICK AUDIT, MUSIC AUDIT BACKFILL, SONIC AUDIT, social-music imports,
TikTok One discovery state, and another Audio Archive run are unsupported
sources. A project directory, creator, topic, TikTok URL, sound URL, master
database, or bare post-ID list is not source authority by itself.

Before browser access, the runner reads the source database and its bound
workspace-global master database locally. It validates the completed run and
freezes either all evidence-ready records (`--all-evidence-ready`) or the exact
repeatable `--post-id` subset. Those options are mutually exclusive. The
frozen ordered set records each original ordinal, post ID, canonical URL,
owner, media type, source evidence snapshot ID/hash, and corresponding master
snapshot identity/hash. Only evidence-ready public video posts are eligible;
photo posts and incomplete, missing, or hash-invalid records receive bounded
local reasons.

The resolved `--source-database` must be the real canonical project-state file,
and `--master-database` must resolve to the same global master path already
bound by that source run. A copy, alternate registry, symlink/reparse-point
escape, or path mismatch fails closed before run-directory creation.

The set is immutable. The runner never searches TikTok, discovers related
content, expands a topic or creator, substitutes a post, refreshes evidence, or
adds a post on resume. An unavailable post remains a terminal per-item failure.
Source or master state changing after freeze cannot silently rewrite lineage
or scope.

The source workflow database and master registry are read-only. Audio Archive
must not add observations, change `last_seen`, alter known-post status, repair
source evidence, create AI state, or change publication history.

## Per-run human authorization

Each `run` requires a fresh affirmative authorization from a named human for
that exact source database, source run ID, frozen post set, storage mode, and
retention terms. Authorization for MUSIC AUDIT, LISTEN, AUDIT, ENGAGE,
BACKFILL, SONIC AUDIT, a previous archive, or a previous model turn is not
reusable.

The authorizing human supplies `--authorized-by <name>` and the explicit
`--authorize-audio-storage` gate. Permanent mode also requires exactly one of
`owned`, `licensed`, or `written-permission` as `--rights-basis` and rejects
`--retention-days`. Public-research mode requires the `public-research` rights
basis and `--retention-days` from 1 through 30. An AI may faithfully pass an
explicit human choice, but must never invent a name, rights basis, permission,
or retention period.

Authorization state is limited to normalized human name, authorization time,
mode, rights basis or expiry, and the exact authorized-scope hash. It does not
store identity documents, contracts, credentials, or unrelated personal data.
`resume` reuses that immutable manifest authorization and cannot create a new
run, scope, mode, TTL, or authority.

## Browser and acquisition boundary

Only `run` and `resume` may access TikTok. They own the canonical social-browser
preflight and start or reuse Microsoft Edge's existing user-data root with
profile directory `Profile 7` in `existing_profile_attach` mode. Before
acquisition they verify the debugging connection, exact profile directory,
TikTok authentication, and observed account; when `--expected-account` is
supplied, it must match exactly. They keep shared Edge running and close only
tabs they create.

The workflow must not use Edge Default, a temporary or replacement Playwright
profile, the in-app browser, or a separately started browser command. Resume
uses the same frozen run and does not gain a new authorization or retry budget
because a process or model restarted.

Acquisition is bounded to media required to derive each selected post's audio.
Each post is acquired from TikTok at most once per item attempt. That one
transient source is normalized locally into a matched durable pair:

- an M4A container containing AAC-LC at 192 kbit/s and 44.1 kHz; and
- an MP3 containing MPEG-1 Layer III audio at 192 kbit/s and 44.1 kHz.

The MP3 is derived locally from the same normalized acquisition path; it must
never trigger a second TikTok fetch. Downloaded source video, source-format or
demuxed audio, decoded PCM, transcoder scratch data, partial output, and every
other temporary media file must be deleted after success and after any failure
or interruption. Only a validated, matched `.m4a` plus `.mp3` pair may leave
the temporary directory.

Audio Archive never retains signed media URLs, request/response headers,
cookies, tokens, HTML, DOM captures, raw network responses, source videos,
thumbnails, or browser diagnostics containing authenticated material.

## Output layout and naming

Every archive run is isolated under:

```text
comments_data/audio_archive_runs/<semantic-run-id>/
  run.lock
  manifest.json
  state.json
  records/
    0001_<numeric-post-id>.json
    0002_<numeric-post-id>.json
  audio/
    0001_<numeric-post-id>.m4a
    0001_<numeric-post-id>.mp3
    0002_<numeric-post-id>.m4a
    0002_<numeric-post-id>.mp3
  review.json
  review.md
  purge.json                         # optional, after expiry purge
```

The semantic run ID includes storage mode, a normalized source-project
descriptor, frozen scope, timestamp with microseconds, and a short
deterministic hash. Long components are deterministically shortened with a
hash; uniqueness is never obtained by dropping mode, scope, time, or hash. The
numeric ordinal is the immutable one-based source order. The numeric post ID
is the only content-derived filename component. Captions, creator display
names, track titles, and arbitrary text never become path components.

Each record binds post identity, source/master lineage, authorization-scope
hash, the two normalized-media contracts, and a separate byte length, duration
when available, and SHA-256 digest for both M4A and MP3. It also binds the
pair's terminal outcome and bounded failure reason. The manifest binds the
ordered record set and hashes. State and reviews summarize progress and
validation without copying captions, comments, transcripts, or semantic
analysis.

No archive artifact may be written into a source project directory, the
workspace root, generic exports, or the master registry. The whole output root
is runtime data and must remain ignored by Git.

`run.lock` is the per-run concurrency control file. Every `run`, `resume`,
`status`, `validate`, and `purge-expired` operation holds the same non-blocking
exclusive OS lock for its full operation. A second operation against that run
fails closed and neither reads mutable state nor changes an audio pair.

## Resume, validation, and tamper rules

Run creation writes the frozen source and authorization contract before media
access. Checkpointing is per item and atomic at the pair boundary: a record is
complete only after both normalized files are closed, independently validated
and hashed, and committed to their final paths as one logical unit. If either
format cannot be produced, validated, or promoted, neither format counts and
no half-pair may remain at a final path. A partial, unvalidated, or unpaired
file never counts.

`resume` may continue only the same manifest and unfinished frozen items. It
verifies the manifest, state, ordered scope, source/master lineage, completed
records, normalized media, and file hashes before browser access. It must not
overwrite or silently repair a completed pair whose bytes, per-format hashes,
or record changed.
A hash, lineage, authorization, path, or contract mismatch fails closed as
tampering and preserves the run for review.

A run is complete only after every frozen item has a terminal record
and both files for every successful pair validate against their independent
recorded sizes and hashes and the manifest.
Unavailable or failed items remain visible in final counts. Partial success
must never be described as archiving the entire source run.

`status`, `validate`, and `purge-expired` are local, offline operations. They
must not start or attach to a browser, contact TikTok, call AI/providers, or
write source/master databases. Validation recomputes all durable hashes and
checks the exact layout; it never redownloads or replaces audio.

## Expiry and purge

Every public-research manifest stores an absolute expiry time derived from its
authorized 1-30 day TTL. Status and validation report expiry. An expired run
cannot resume or be treated as usable research data.

`purge-expired --run-id` may delete only the M4A and MP3 files in that exact
expired public-research run. It resolves the run beneath
`comments_data/audio_archive_runs/`, verifies the manifest and every target's
path and pre-deletion SHA-256 hash, and rejects symlinks, reparse-point escapes,
extra paths, hash mismatches, permanent runs, and unexpired runs. It never
accepts a workspace root, glob, creator, topic, or arbitrary directory as a
deletion target.

After purge, the runner retains only minimal manifest/state, per-item deletion
tombstones, reviews, and `purge.json` needed to prove which hash-bound files
were removed and when. Purging does not alter source evidence or make posts new
again. Temporary acquisition files are cleaned in both modes on every outcome
rather than waiting for expiry.

## CLI

Use the required workstation interpreter, not bare `python` or `py -3`:

```powershell
$Python311 = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$MasterDb = 'comments_data\tiktok_master\state\tiktok_master.sqlite'
$ArchiveRoot = 'comments_data\audio_archive_runs'
```

Permanently retain all eligible audio from one completed authorized run:

```powershell
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot run `
  --source-database '<absolute project workflow database path>' `
  --source-run-id '<completed run id>' --all-evidence-ready `
  --storage-mode permanent --rights-basis licensed `
  --authorized-by '<named human>' --authorize-audio-storage `
  --expected-account '<TikTok handle>'
```

Retain an exact subset for seven days as public research:

```powershell
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot run `
  --source-database '<absolute project workflow database path>' `
  --source-run-id '<completed run id>' `
  --post-id '<numeric post id>' --post-id '<numeric post id>' `
  --storage-mode public-research --rights-basis public-research `
  --retention-days 7 --authorized-by '<named human>' `
  --authorize-audio-storage
```

Continue the immutable run, or inspect it offline:

```powershell
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot resume `
  --run-id '<audio archive run id>' --expected-account '<TikTok handle>'
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot status `
  --run-id '<audio archive run id>'
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot validate `
  --run-id '<audio archive run id>'
& $Python311 .\audio_archive_tiktok.py `
  --master-database $MasterDb --output-root $ArchiveRoot purge-expired `
  --run-id '<expired public-research run id>'
```

Global database and output options precede the subcommand. Only `run` accepts
storage authorization, source selection, mode, rights basis, and retention.

## Closed capability boundary and backup warning

Audio Archive performs no discovery, evidence/comments/transcript collection,
catalog lookup, fingerprinting, acoustic analysis, song identification,
semantic AI, drafting, approval, engagement, publication, or outbound TikTok
action. It does not upload audio to Cyanite, Mirelo, AcoustID, or another
provider. Success proves only that normalized bytes were acquired from the
bound post at that time; it does not prove song identity, ownership, license,
originality, or platform-wide availability.

Both audio formats, state, manifests, records, reviews, and purge receipts are
excluded from Git. Git is not a media or evidence backup. If recovery is
required, use
encrypted, access-controlled storage that preserves hashes and expiry
metadata. Public-research backups and restored copies retain the original
expiry and must be purged from backup generations; copying never extends TTL.
Permanent archives still require ongoing rights, access, retention, and
deletion governance.
