# Offline master registry repair

`master_registry_repair.py` repairs the specific historical damage caused by
the retired MD5 source-ID rewrites. It has no browser, collection, analysis,
approval, or publication path. Do not run it concurrently with live workflows.

The repair restores only source identity rows. Every existing run, snapshot,
post, comment, evidence hash, analysis record, publication attempt and approval
remains unchanged. It does not redirect local runs to a different master,
rehash handoffs, reopen completed runs, or clear duplicate fences.

## Proof and transaction boundaries

Planning opens the master query-only and requires each damaged, unreferenced
MD5 row to map uniquely to an orphaned canonical SHA-256 identity. Candidate
paths are limited to the current path and the corresponding previous-root
path. Existing master run IDs must agree with that original source identity.
Unexplained orphan references, ambiguous identities and triggers stop planning.

For a relocated file, the complete local run set, project/workflow identities
and evidence-ready post/hash set must match the historical master records.
Duplicate identities and copies without a matching historical run are rejected.
Missing moved files receive no alias; their historical source identity is
restored and their original location remains authoritative.

An explicit location alias preserves the original source ID and identity path
while designating one verified current file. Registration and lineage checks
resolve that binding without regenerating immutable IDs. The alias checksum is
an integrity check, not an authorization signature. The previous location
cannot simultaneously be registered as another mutable copy of that source.

The v3 plan fingerprints every stored value in every master table. It scans
table pages sequentially, serializes primitive row values with pickle protocol
5 and memoization disabled, hashes individual rows, sorts their fixed-size
digests, and hashes that sequence with the table definition. It never
unpickles data. The plan and receipt bind the exact CPython version and
serialization settings; apply rejects a different runtime or format. Row
insertion order and Python object sharing do not matter; duplicate
multiplicity, SQLite value types and all stored values do.

Apply requires a matching consistent backup and the frozen original database,
or a separate copy when `--rehearsal` is specified. Hardlinks cannot substitute
for independent files. Inside one `BEGIN IMMEDIATE` transaction, apply checks
the frozen data, independently repeats the identity proof, rechecks relocated
files, restores source rows and inserts aliases. It verifies every protected
table before committing a durable repair receipt. Any failure rolls back.
Repeated apply verifies the current database against its receipt before
reporting `already_applied`; it never silently reapplies a stale plan.

## Procedure

Use this workstation's required interpreter in PowerShell:

```powershell
$taskPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
```

1. Finish existing collectors/publication workers. Preserve their exact state.
2. Create a consistent standalone copy using SQLite's backup API. Retain that
   pre-repair copy unchanged and use a second copy for the rehearsal.
3. Encrypt the backup and verify it before the actual repair.
4. Create a no-clobber plan with the exact current and previous roots.
5. Apply the plan to the rehearsal copy, review the receipt, and run SQLite
   integrity validation. Then apply the same plan to the original database.
6. Retain the plan, both receipts and verified backup outside Git. Run offline
   lineage/registry validation; a data repair is not a live publication test.

Example arguments (replace the uppercase placeholders with exact paths):

```powershell
& $taskPython -B master_registry_repair.py plan --database MASTER --current-root CURRENT_ROOT --previous-root PREVIOUS_ROOT --output PLAN.json
& $taskPython -B master_registry_repair.py apply --plan PLAN.json --database REHEARSAL_COPY --backup BEFORE_COPY --rehearsal --output REHEARSAL_RECEIPT.json
& $taskPython -B master_registry_repair.py apply --plan PLAN.json --database MASTER --backup BEFORE_COPY --output APPLY_RECEIPT.json
```

## Encrypted local backups

`tools/encrypted_registry_backup.py` uses streaming AES-256-GCM and Windows
DPAPI under the current user. Install the maintenance dependencies from
`requirements-dev.txt` in the intended environment if absent. The input must
already be a consistent SQLite copy; encryption does not snapshot a live file.

```powershell
& $taskPython -B tools/encrypted_registry_backup.py encrypt --source BEFORE_COPY --target BACKUP.sqlite.aesgcm
& $taskPython -B tools/encrypted_registry_backup.py verify --source BACKUP.sqlite.aesgcm
& $taskPython -B tools/encrypted_registry_backup.py restore --source BACKUP.sqlite.aesgcm --target NEW_RESTORE.sqlite
```

Keep the ciphertext and adjacent `.aesgcm.json` receipt together. The receipt
contains the DPAPI-protected key and authentication parameters. Recovery
requires the same Windows user/profile; this is not a portable machine-loss
backup. Restore creates a new file exclusively and removes that new output if
authentication or checksum validation fails. It never overwrites a database.
Never replace a running workflow's master with a restored copy.

The retired `fix_master_source_id.py`, `fix_master_foreign_keys.py`,
`fix_master_sources.py`, and `fix_master_db_path.py` entry points now exit without
opening SQLite. Original bytes are retained locally as non-executable text
under the ignored `legacy/one_off_state_mutators/` archive. Do not use them as
recovery commands.
