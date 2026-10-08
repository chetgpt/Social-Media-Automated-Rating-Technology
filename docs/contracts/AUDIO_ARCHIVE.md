# Audio Archive Workflow

## Purpose

`audio_archive_tiktok.py` downloads the soundtrack of TikTok posts that are
already present in a local project database. Each successful post produces:

- one normalized M4A file; and
- one MP3 derived locally from that same acquisition.

The workflow does not discover posts, run a new MUSIC AUDIT, analyze audio,
engage with a post, or publish anything. Source databases are opened read-only
and archive output is stored separately under
`comments_data/audio_archive_runs/` by default.

## Simple operating rule

A direct user request to run AUDIO ARCHIVE is sufficient. There is no separate
per-run authorization statement, rights-basis flag, storage-mode decision,
retention period, master-database match, or original-path requirement.

The source may be any compatible current, incomplete, copied, moved, restored,
reorganized, or legacy project SQLite database that contains
`engage_tiktok_runs` and `engage_tiktok_posts`. A run
does not need a particular terminal status. Every row already marked
`evidence_ready=1` is eligible. Rows with no usable canonical TikTok post URL
are skipped and reported instead of blocking the other rows.

## One database run

Use the required Python 3.11 interpreter:

```powershell
$Python311 = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'

& $Python311 .\audio_archive_tiktok.py run `
  --source-database 'comments_data\project_name\state\engage_state.sqlite' `
  --source-run-id 'engage_exact_run_id'
```

With no selection option, all evidence-ready posts in the source run are used.
To archive only particular posts, repeat `--post-id`:

```powershell
& $Python311 .\audio_archive_tiktok.py run `
  --source-database 'comments_data\project_name\state\engage_state.sqlite' `
  --source-run-id 'engage_exact_run_id' `
  --post-id '1234567890123456789' `
  --post-id '1234567890123456790'
```

`--all-evidence-ready` remains accepted for readability but is optional.
`--expected-account` is also optional; omit it to use the authenticated TikTok
account already present in Edge Profile 7.

## Multiple projects or every project

`run_audio_archive_batch.py` is the normal batch entry point. It discovers
compatible databases and runs them sequentially. It continues past a failed
source and records the failure. A completed archive covers only the exact rows
frozen in its manifest; if a source later gains evidence-ready rows, the batch
creates bounded delta archives for only the uncovered rows. It resumes an
unfinished archive only when its project and frozen post identities match.

Preview every project without touching the browser or writing output:

```powershell
& $Python311 .\run_audio_archive_batch.py --dry-run
```

Archive every compatible project:

```powershell
& $Python311 .\run_audio_archive_batch.py
```

Archive only selected projects by repeating `--project`:

```powershell
& $Python311 .\run_audio_archive_batch.py `
  --project project_one `
  --project project_two
```

An optional repeatable `--run-id` narrows the plan further. Project filters are
not a special allowlist; any compatible project directory can be supplied.
Omitting them always means all projects under `comments_data`.

Batch summaries are written beneath
`comments_data/audio_archive_runs/batches/`. A source with zero evidence-ready
rows is reported as skipped rather than treated as a workflow failure.

## Output and continuation

Each source run receives an isolated directory:

```text
comments_data/audio_archive_runs/<audio-archive-run-id>/
  run.lock
  manifest.json
  state.json
  .staging/
  records/<ordinal>_<post-id>.json
  audio/<ordinal>_<post-id>.m4a
  audio/<ordinal>_<post-id>.mp3
  review.json
  review.md
```

The manifest freezes the selected post IDs and URLs. Each record stores
separate file sizes, durations, and SHA-256 hashes for M4A and MP3. Temporary
download and conversion files are removed after success or handled failure.
The source database is never modified.

Continue or inspect a single archive with:

```powershell
& $Python311 .\audio_archive_tiktok.py resume --run-id '<archive-run-id>'
& $Python311 .\audio_archive_tiktok.py status --run-id '<archive-run-id>'
& $Python311 .\audio_archive_tiktok.py validate --run-id '<archive-run-id>'
```

`run` and `resume` own Edge Profile 7 startup and TikTok authentication.
`status`, `validate`, and batch `--dry-run` are offline.

## Minimal integrity rules

The simplified workflow keeps only safeguards needed for reliable output:

- source SQLite files are opened read-only/query-only;
- only evidence-ready rows with a valid canonical TikTok post URL are used;
- one TikTok acquisition supplies both formats;
- an item counts as archived only when both M4A and MP3 validate;
- archive runs use isolated directories and a per-run lock;
- resume uses the frozen manifest and never requires the old source path or
  master database to still exist;
- no AI, acoustic analysis, engagement, or publication is performed.

Audio files and runtime state remain outside Git. Back them up separately if
they need to be retained.
