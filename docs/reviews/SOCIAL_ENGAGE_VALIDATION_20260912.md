# Threads-first and LinkedIn ENGAGE validation

The expansion adds official-API collection and a shared local sequence for
analysis, drafting, independent review, presentation, human authorization and
guarded text replies/comments. Threads is the primary entry point;
LinkedIn is restricted to verified administered organization Pages. Existing
TikTok source and the older LinkedIn collection engine were not modified.

## Validation evidence

- 53 new adapter/workflow regression tests pass on the final implementation.
- All 44 existing LinkedIn API/state/workflow tests passed in the preceding
  combined run. Those legacy modules were unchanged by this expansion.
- Both platform capability commands and the Threads CLI help run offline.
- Git whitespace validation passes for all staged source and documentation.
- Tests use synthetic injected transports, accounts, tokens, approvals and
  temporary SQLite fixtures. No live social API or browser access occurred.

Coverage includes exact count and immutable resume, global known-post fences
inside each isolated database, time-window eligibility, account/owner binding,
cursor-only paging, metadata projection and token redaction, offline AI stages,
independent reviewer identity, stale hashes, type-specific disclosure/rating
policy, shadow/live separation, token rotation/one-time use, operator binding,
OS/SQLite concurrency, current duplicate checks, pre-submit expiry, uncertain
outcomes, receipt readback, retention and explicit deletion.

An independent bounded source review identified lifecycle gaps. The final
implementation adds explicit shadow-to-live intent with fresh presentation,
append-only per-post evidence refresh with downstream invalidation, and
historical exact-collection verification separate from current availability.
Regressions cover an expired/deleted sibling not preventing a refreshed
unsubmitted post from proceeding, and explicit deletion after expiry still
preventing recollection. Revisions preserve their original TTLs.

## Limits

This is source and synthetic validation, not a live acceptance test. App
approval, actual scopes, credential availability, target accessibility and
platform acceptance remain to be checked for the intended account. No token
was obtained or read, no post was collected, no human approval was recorded,
and no reply/comment was published during implementation.

Music declarations, transcripts, acoustic/lyrics analysis, media acquisition,
DMs and showcase posts remain unsupported in these adapters. AI results are
produced by the interactive assistant, not generated automatically by a CLI
stub. An uncertain submission cannot be automatically retried or cleared.
See the full workflow contract for scope, setup and retention obligations.

The original working folder's local browser/collector variants were saved
separately before expansion in source backup commit
`e2742b955eb6162d0b1770f26123cff22ee30ee4`, pushed to both GitHub and the local
bare backup. Development is based on merged release `e1f6c6e`; those unreviewed
variants were not imported into the new feature branch.
