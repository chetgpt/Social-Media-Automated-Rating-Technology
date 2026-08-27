# Documentation layout

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
