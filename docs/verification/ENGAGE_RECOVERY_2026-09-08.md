# ENGAGE CAPTCHA and worker recovery — 8 September 2026

The implementation applies to all TikTok ENGAGE topics and both plain and native-mention comments. Runtime changes were tested offline. No live browser restart, CAPTCHA interaction, comment submission or modification of existing run databases was performed for this fix.

## Implemented behavior

- Shared visible-challenge detection during ENGAGE publication/probe preflight, navigation, composition, control polling, pre-submit checks and post-submit verification. Challenges preserve the verified Profile 7 tab for human completion and emit structured diagnostics. Closed/unresponsive targets remain distinct errors.
- Bounded browser operations, stage limits and cleanup. Both executable publisher and probe have an owned-process supervisor. Publisher progress includes phase, heartbeat and durable publication/attempt/process identity. Only the owned child may be stopped on a deadline; shared Edge and unrelated workers are excluded.
- Local and master publication confirmation commits immediately after validated current-attempt creation evidence, before screenshots/reload. Later auxiliary updates keep confirmation and update the existing receipt rather than creating another attempt.
- Durable claim ownership, written with the reservation transaction. The offline `engage_publication_recovery.py` command releases only proven-dead pre-submit workers. Submit-intent interruptions remain uncertain; confirmed comments allow capture recovery only. Legacy ownership gaps and process-inspection uncertainty block release.
- The unsafe queue `INSERT OR REPLACE` is removed. A schema migration preserves old queue rows, receipts, indexes and triggers. Exact handoff replay is idempotent. Explicit eligible supersession preserves old IDs, prior status and linkage; confirmed/active/uncertain records remain fenced.
- Shared browser startup is serialized with a process lock. The UI bridge uses one monotonic deadline across its window/control waits. Exact-comment capture uses bounded ID-first lookup with exact text verification and specific missing/ambiguous-row reasons.
- Google instructions now describe generic topics and the recovery commands, including stopping a batch when CAPTCHA appears after an already-confirmed comment.

## Validation

Final regression result: **494 passed in 64.97 seconds**.

Regression coverage includes current-attempt request correlation; CAPTCHA before and after submit; no submit on pre-submit challenge; challenge-tab preservation; primary error preservation; hung observations and cancellation-resistant operations; real isolated Python worker timeouts; PID-reuse/unknown-liveness protections; interrupted claims before/after submit intent; confirmation surviving auxiliary failure/cancellation; same-text supersession and rollback; migration history preservation; and browser startup lock/deadline behavior.

The verification command is:

```powershell
& 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe' -m pytest `
  tests/publication tests/workflows/test_engage_tiktok.py `
  tests/workflows/test_engage_handoff_history.py `
  tests/workflows/test_engage_publication_recovery.py `
  tests/workflows/test_engage_account_resolution.py `
  tests/integrations/test_social_browser.py `
  tests/integrations/test_social_browser_startup_guard.py `
  tests/workflows/test_analysis_workflow.py -q
```

All affected Python modules compile. CLI help for recovery, explicit supersession and the probe resolves without browser access. Eight consecutive isolated supervisor-worker starts completed successfully. A read-only call to the new status command against Kaden publication `d826e2ca120344aae4a93b155025ea85` returned `confirmed_capture_recovery_only`, retaining master attempt `ae5a9248542bed61cd57a216b3524f62` as confirmed.

## Remaining practical limits

Live TikTok UI behavior still requires observation during a future authorized run; offline tests cannot guarantee detection of every future challenge layout. Human completion remains necessary. The offline reconciler does not establish remote absence or retry uncertain comments. It cannot invent ownership for historical interrupted workers. Existing removed queue records are not reconstructed, and existing databases migrate only when the ordinary workflow next initializes their schema. Recipient notifications and remote native-mention persistence require separate evidence and are not implied by a creation receipt.

Operational instructions: [ENGAGE publication recovery](../contracts/ENGAGE_PUBLICATION_RECOVERY.md).
