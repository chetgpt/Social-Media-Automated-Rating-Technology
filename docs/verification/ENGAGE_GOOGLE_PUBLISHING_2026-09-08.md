# Google guitar publishing preparation, 8 September 2026

The repaired native composer passed the maintained nonpublishing probe on Ben
Newport's post `7619267461313072406`, selecting the exact candidate username
`guitar.les`. Its observed native display label is **@guitar tutorials**.
Both production-composition rehearsals passed exact text/entity verification.
This verifies native composition; no server-side comment, notification or
engagement result is claimed.

## Observed browser results

All attempts used the existing Edge Profile 7 session and verified `kitascore`.
Each report recorded zero blocked publication requests, cleared its editor and
closed only its temporary tab. Reports live under `local_artifacts/reports/`.

| Report | Total | Preflight | Outcome |
|---|---:|---:|---|
| `guitar_native_diagnostic_20260908.json` | 88.813 s | 42.047 s | Exact account selected/clicked; waiting for suggestion disappearance timed out at 5.125 s. |
| `guitar_native_insertion_check_20260908.json` | 78.188 s | 25.844 s | No visible suggestions appeared within the bounded observation. |
| `guitar_native_editor_check_20260908.json` | 87.844 s | 28.750 s | Screenshot established a TikTok verification puzzle; editor unfocused and empty. Live retries stopped for manual verification. |
| `guitar_native_after_verification_20260908.json` | 95.609 s | 30.750 s | After the user completed verification, the exact suggestion was clicked but no native entity was inserted within five seconds. |
| `guitar_native_click_layout_20260908.json` | 84.985 s | 29.141 s | Passed label observation and both native-composition cases. |

The passing report observed insertion in 0.063 s during label observation,
then 0.266 s and 0.860 s in its two composition cases. Entire cases took
5.766 s and 8.078 s. The selected row was a visible
`data-e2e="comment-at-list"` `DivMentionSuggestionItem`. Its center hit the
same row. No selector substitution or first-result fallback was introduced.

The initial wait-for-hidden failure does not prove a native entity existed in
that attempt. The later failed insertion and successful probe show live UI
availability varied. The implementation now verifies positive insertion of a
new editor entity rather than inferring success from list disappearance.

## Implementation and verification

- Production selection snapshots existing native nodes and profile links,
  rechecks the exact observed candidate, clicks it and waits up to five seconds
  for one new native entity or exact-handle profile link. Existing entity
  identities, order, text and link destinations must remain unchanged. Final
  text, label and entity checks still run immediately before submit intent.
- Keyboard composition and cleanup verify that the editor actually received
  focus before using page keyboard input. Overlay-held focus fails without
  sending those keystrokes.
- The probe retains bounded operation/error/timing diagnostics, counts and
  safe selected-row layout metadata. The first failure survives later cleanup
  errors. Optional `--failure-screenshot` captures only the temporary target
  tab before cleanup. No raw transport exceptions are stored.
- `import-creator-matches --native-probe <report>` validates passed reports
  and binds labels automatically. New pending LIVE imports with matches need
  reports. Offline SHADOW/no-match results and completed historical hashes
  remain supported. Label overlap or identical display names do not replace
  exact username identity.
- The Google entry point and adapter now link an explicit preparation,
  independent review, presentation, authorization, handoff and single-comment
  publishing walkthrough. Blanket scripted review approvals remain invalid.

The final combined offline suite passed **338 tests** across creator matching,
native-label binding, single-operator approval, creator mention publication,
plain composition, publication confirmation, native UI and probe diagnostics.
After adding selected-row layout diagnostics, the 77 affected native UI/probe
tests passed again. Required-Python compilation and whitespace checks passed.
The successful real report also passed offline binding against the exact
preserved guitar database using a SQLite read-only connection, with zero writes.

## Google continuation

Use [the publishing walkthrough](../contracts/ENGAGE_GOOGLE_PUBLISHING.md).
The preserved run `engage_4363df2c2e47418f` remains stored with two evidence-ready
posts, zero authorizations and zero publications. Its frozen old matches,
drafts and evidence were not rewritten. The native label learned here must not
be patched into those completed records.

The user can request fresh preparation of the exact two known posts in a new
ENGAGE `refresh_known` run within the same project database. Google must read
the fresh complete evidence, reassess the connection, obtain current native
labels before matching import, draft and obtain a genuine independent critic
decision, then show the exact final comment. After explicit approval, Google
must continue through `authorize`, `handoff`, and the single-publication
`publish_pending.py --execute` command and verify its receipt. No actual
comment publication or secondary showcase was performed by this repair task.
