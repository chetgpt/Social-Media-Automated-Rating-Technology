# Workflow source integration validation

Updated 12 September 2026.

The integration branch brings the saved workflow source into `main`, which
predates the source backup. It includes the publication and registry safeguards
described in `SOURCE_FIX_VALIDATION_20260910.md`, plus the final provider
retirement corrections below.

## Release corrections

New canonical collection and backfill runs default to an empty configured
catalog-provider set. Existing explicit MusicBrainz provider lists still load
and retain their immutable run bindings. The shared collector and direct
adapter now produce a locally hash-bound `unsupported` / `provider_retired`
result without MusicBrainz network access, request-slot reservation, cooldown,
cache lookup, or circuit handling. Apple exact-ID lookup and its pacing,
provenance, and cache behavior are preserved.

Historical catalog results retain their existing nested schema, hashes,
candidate projection, deterministic scoring, and validation behavior. No
production evidence is rewritten by these source changes.

New TikTok One bridge parents use the empty canonical catalog default. Fresh
children use the exact provider list frozen into their parent, including a
historical MusicBrainz list. Existing children still resume by their saved run
ID without scope overrides. Parent settings and their integrity hashes remain
unchanged throughout continuation.

## Validation scope

Validation uses the committed workflow snapshot with the final source edits in
an isolated checkout. Regression databases, media samples, and results are
temporary synthetic fixtures. An inherited test guard blocks production result
and browser-profile access and outbound network calls. Only the reviewed
synthetic Python workers, PowerShell AST tests, local platform-version probe,
and local FFmpeg/FFprobe codec tests may start subprocesses.

Final cumulative coverage is **2,078 current test cases passing**, with no
remaining failures, errors, or skipped cases. This is coverage across a full
run and the affected-module rerun, not a claim that one full-suite invocation
finished green:

- The full run recorded 2,059 passes, four failures, and 14 fixture errors.
- After correcting the remaining catalog assumptions and bridge dispatch,
  all 112 tests in the four affected modules passed. These replace those
  modules' earlier results; 1,966 passing cases from the full run remain valid.
- The rerun includes both new-default and preinitialized legacy-parent child
  creation, incomplete-child resume, unchanged settings hashes, collection
  readiness, publication-window validation, and Top Content state integrity.

Earlier targeted checks also passed: 42 master/catalog-registration tests and
75 enrichment, collection, and backfill-retirement tests. These counts overlap
the final coverage and are not additive. All 11 edited Python files passed AST
parsing and Git whitespace checks. Tested source hashes match the release
source. An independent review found no actionable issue in the four changed
production modules, including retirement shape/hash, absence of provider
transport or pacing, preserved Apple flow, and frozen legacy bindings.

GitHub has no configured CI checks for this branch; local validation and source
review are the merge checks. Detailed machine-readable evidence is retained in
the ignored release artifacts as `final_validation.json`, `final_suite_2.xml`,
and `defaults_followup_1.xml`.

The first full-branch check identified the two retirement defects. Seven other
failures came from the test guard's handling of a Windows subprocess audit
argument; all seven passed after the guard correction. A later run was stopped
before completing because of an argument-ordering mistake in test fixtures;
that mistake was corrected and all edited Python files passed syntax checks.

## Operational boundary

`comments_data/` remains excluded from routine source work and Git backups.
The production registry repair remains paused after its previous stale-plan
rejection. This integration performs no database migration, live collection,
transient audio acquisition, engagement, or publication. A live collection smoke
test requires the user's mode and exact source scope.

The original source backup remains on
`codex/backup-workflows-20260908T122827Z`. The original working branch and staged
content are preserved. Detailed test logs and local receipts remain under
`local_artifacts/github_release_2026-09-10/` and are not Git source.
