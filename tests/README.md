# Test layout

- `unit/` covers reusable contracts, state, enrichment, profile, and trace logic.
- `workflows/` covers canonical workflow orchestration and guarded MUSIC AUDIT.
- `publication/` covers comment/showcase publication gates and receipts.
- `integrations/` covers isolated platform, browser, archive, and importer adapters.
- `sonic/` covers the separate SONIC AUDIT implementation.

Tests remain discoverable through the workspace-root `pytest.ini`.
