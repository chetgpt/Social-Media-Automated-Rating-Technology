# Documentation layout

For the current Instagram internal API, TikTok topic/creator ALL, LinkedIn
comment API, and the two distinct 24/7 implementations, start with the
[September 30 change record](reviews/SOCIAL_COLLECTION_AND_24_7_CHANGES_20260930.md).
It links implementation details, bounded validation results, operating guides,
and unresolved limitations without requiring runtime corpus inspection.

The workspace authority and primary operator guides intentionally remain at the
repository root:

- `AGENTS.md`
- `README.md`
- `WORKFLOWS.md`
- `GEMINI_3_1_PRO_WORKFLOW.md`

Supporting capability contracts live under `contracts/`. Retired instructions
and historical workflow notes live under `legacy/` and are never active
operator guidance.

Cleanup and migration records live under `cleanup/` so structural changes can
be audited without relying on Git history alone.
