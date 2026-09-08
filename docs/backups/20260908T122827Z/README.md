# Workflow source backup — 8 September 2026

Backup branch: `codex/backup-workflows-20260908T122827Z`. Parent commit: `4e86ef9585408cb51402dbcddca9d0fdff89b67c`.

This checkpoint preserves the working source before the proposed corrective work. It includes 329 existing/reviewed source files for ENGAGE SHADOW/LIVE, MUSIC AUDIT/LISTEN, AUDIT, PULSE, POSTS DISCOVERY, multi-topic MUSIC AUDIT, backfill, SONIC, AUDIO ARCHIVE, showcase publication, Profile 7 management, LinkedIn, social-music imports and TikTok One discovery. Skills, contracts, tests, reusable configuration and retained legacy source are included. It does not authorize running any mode or publishing any response.

`source-manifest.json` records Git blob IDs, SHA-256 values and the files left local. `environment.json` records installed package/tool versions without environment-variable values or authentication material. This is an observed environment inventory, not a dependency lock or fresh-install test.

## Restore source

Use the verified local bare repository as a source, and clone into a new empty folder to avoid overwriting the current workspace:

```powershell
git clone --branch codex/backup-workflows-20260908T122827Z 'D:\Kita Co. Lab\Git Backups\TEST Tiktok Scraper Modules\local-backup.git' '<new-empty-restore-directory>'
```

Verify the resulting commit against the external backup receipt. The archive includes historical source that remains noncanonical; `AGENTS.md` takes precedence over older examples. The operator under `.agents/skills/` is required application source. Use the designated Python 3.11 executable and retain the normal exact-run, freshness, approval and publication guards. Restoring source alone does not restore authenticated Edge or authorize a new collection/publication.

## Deliberate exclusions

The backup follows `docs/contracts/SOURCE_SCOPE.md`: no `.env`, OAuth/API credentials, cookies, browser profile, SQLite evidence/approval databases, raw media, captures, receipts, generated comment bodies or diagnostic dumps. Existing runtime data remains local and needs a separate SQLite-consistent backup for data recovery. Root one-off database mutators, scratch runners, and the obsolete `AUDIO_ARCHIVE (1).md` contract remain outside this source snapshot. Two evidence-bearing historical verification reports containing exact draft/published text remain local; source documents may link to those locally available reports. Reusable technical verification notes are included.

## Checkpoint validation and limitations

- 255 Python files in this exact snapshot parsed successfully.
- Earlier analysis ran the isolated `tests/unit` suite: 486 passed in 158.47 seconds. This was not a fresh full-suite run of the snapshot.
- Credential-pattern matches were reviewed as placeholders or synthetic test data.
- Known registry and publication-control issues are preserved for subsequent work; this checkpoint is not a clean-bill-of-health claim.
- The configured GitHub `origin` is public, despite older private-repository wording. This local checkpoint grants no permission to publish it publicly.
- Observed environment differs from requirements: `google-auth` is 2.29.0 while `requirements.txt` requests >=2.35; `types-requests` is absent. No dependencies were changed during backup.
