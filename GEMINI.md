# Google model workspace entry point

Read `AGENTS.md`, `WORKFLOWS.md`, and `GEMINI_3_1_PRO_WORKFLOW.md` before
operating in this workspace. The Gemini adapter contains the execution
walkthrough; the current user's instructions and the binding workspace
contract govern scope and permission.

For ENGAGE creator connections, also read
`docs/contracts/ENGAGE_CREATOR_MENTIONS.md`. Use the canonical offline
analysis, creator-matching, drafting and independent-review queues. Read all
available post captions, transcripts, subtitle segments/statuses and collected
comments/replies, including the matching and critic `context_evidence`.
Write a specific useful comment grounded in the post and its discussion;
compare drafts to avoid generic or interchangeable responses. Explain each
creator connection with direct content evidence from both posts and relevant
discussion context. Missing evidence must remain a limitation.

Up to two strong same-run matches are allowed only for positive-support
comments. No match is valid. SHADOW stops after reviewed storage. LIVE still
requires exact-text presentation, the user's approval and all publication
checks. Loading this file does not resume a stopped run or authorize posting.

For a test request to paste into Google, use the "Google model SHADOW test"
example in `GEMINI_3_1_PRO_WORKFLOW.md`.

The executed baseline and measured limits are in
`docs/verification/ENGAGE_2026-09-07.md`. Do not copy its scores or comments into
new posts. Before freezing native-ready matches, use the separate nonpublishing
`engage_mentions_probe.py` to observe exact username-to-label bindings. Keep new
mention comments in one paragraph. A display name, first suggestion, plain
`@handle`, different actor string, or `independent_review=true` alone proves
neither a correct tag nor an independent semantic review.

For LIVE creator comments, follow `docs/contracts/ENGAGE_GOOGLE_PUBLISHING.md`.
For CAPTCHA or a stalled/interrupted publisher, also follow
`docs/contracts/ENGAGE_PUBLICATION_RECOVERY.md`. Wait for manual verification in
the preserved Profile 7 tab, inspect durable worker/attempt state before retrying,
and never overwrite an old publication row to resolve a duplicate draft.
Pass successful native rehearsal reports with repeatable `--native-probe`
arguments to `import-creator-matches`; the importer binds labels before freezing.
Do not generate blanket review approvals. After the user approves the exact
reviewed comment, complete `authorize`, `handoff`, and the single-publication
`publish_pending.py --execute` command, then verify the receipt.

The user is the sole operator. Never ask for a personal name: omit
`--presented-to` and `--authorized-by` for ENGAGE; both default internally to
`workspace-operator`. This does not approve a comment. Show the exact reviewed
text, receive the user's explicit approval, then use the bound token normally.

The historical 7 September live-test preparation is documented in
`docs/verification/ENGAGE_LIVE_2026-09-07.md`. That report's outcome applies to
that attempt; inspect the latest run and durable receipt for current status.
