# Google: publishing an ENGAGE creator comment

Google/Antigravity uses the same canonical publisher as Codex. Native creator
mentions require a successful composer rehearsal before matching is frozen.
The model must then supply grounded content and an independent critic pass.
Only the user's approval of the exact shown comment permits submission.
This workflow applies to any TikTok ENGAGE topic. Topic-specific examples below
are historical evidence, not required subjects, handles or default scopes.
For CAPTCHA, stalled workers and interrupted publication, follow
[Publication recovery](ENGAGE_PUBLICATION_RECOVERY.md).

The [8 September verification](../verification/ENGAGE_GOOGLE_PUBLISHING_2026-09-08.md)
records a successful live native-tag rehearsal for `guitar.les`, which displayed
as `@guitar tutorials`. It is an observed example, not a hardcoded default.

## Prepare the exact post and candidate

Read `AGENTS.md`, `WORKFLOWS.md`, `GEMINI_3_1_PRO_WORKFLOW.md` and
`ENGAGE_CREATOR_MENTIONS.md`. Use the required Python executable:

```powershell
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
```

Inspect the stored run first. Keep its exact database, master database,
run ID and account. An unfinished collection continues with `resume-collect`;
do not create a replacement collection as recovery. Offline analysis, matching,
review, presentation and authorization do not start or check the browser.

If a completed run has missing frozen labels or needs new analysis/review, do
not relabel or reanalyze it in place. If its evidence is outside the default
60-minute publication window, preserve that history; never reset rows, change
timestamps, extend freshness or reuse an old approval for changed bindings.

When the user requests fresh publication preparation for selected exact posts,
use a separate explicit ENGAGE `refresh_known` run. This is a new evidence
observation of the known IDs, retaining the master duplicate ledger. Use a new
run in the existing project database, binding the same master path read from
the stored run. For a selected source and one candidate, freeze their two IDs:

```powershell
# Set $Database and $MasterDatabase from the existing run, not guessed defaults.
& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase collect `
  --project $Project --topic $Topic --posts 2 --workflow engage --mode live `
  --collection-policy refresh_known `
  --refresh-post-id $SourcePostId --refresh-post-id $CandidatePostId `
  --expected-account $Account
```

Run this once only after that fresh-preparation request; retain its returned
run ID and resume that same ID if collection is incomplete. Keep new queue and
probe files in a run-specific subdirectory so old artifacts are preserved.
The `collect` command owns Profile 7 preflight and exact account verification.
Both posts must reach evidence-ready before analysis and matching.

Read each complete fresh packet. Do not claim demonstrations, pacing or other
visual details when visual evidence is unavailable.
Compare the actual supported content. Shared topic hashtags alone
do not justify a strong match. If the pair does not meet the matching rubric,
return a no-match outcome and explain it; do not invent a connection to fill it.

## Bind native labels before import

After analysis, export the complete matching corpus and prepare semantic
matching results. For the selected candidate, run the maintained nonpublishing
probe with the selected source URL and exact candidate handle:

```powershell
& $EngagePython .\engage_mentions_probe.py `
  --url $SourcePostUrl --handle $CandidateHandle --expected-account $Account `
  --report (Join-Path $RunDirectory 'native-mention-probe.json')
```

If reciprocal comments are selected, observe both exact candidate handles.
One successful label may be reused for the same candidate within this run.
Do not import a pending LIVE selection with mentions until its probe passes.
On failure, read `diagnostics.failure.operation`, `code`, `creator_handle`
and measured times. Correct the observed cause before another attempt. The
first failure survives a later cleanup failure. Do not expose raw transport
exceptions, guess labels or click a different username.
If input focus or suggestions fail, inspect `failed_editor` and optionally a
`--failure-screenshot <png>` from the same probe. A visible verification puzzle
requires manual user completion in the same Profile 7 session; stop retries
until the user has completed it. Do not automate the challenge.

```powershell
& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase import-creator-matches `
  --run-id $RunId --file $CreatorMatchResults --actor antigravity-gemini31-matcher `
  --native-probe (Join-Path $RunDirectory 'native-mention-probe.json')
```

Repeat `--native-probe` for additional successful reports. The importer checks
the account, same-run source, observed identities, two passed composition cases
and cleanup. It adds exact observed labels without modifying the input file.
A conflicting supplied label rejects the batch. The report is preparation
evidence; it is neither human approval nor proof of a future server-side tag.

## Review, present, authorize and publish

Use the canonical draft and review queues. Draft a concise, specific comment
from the complete packet; code appends the frozen matches and rating. Give a
separate critic context the full source and candidate evidence and all drafts.
Require actual per-post reasons and issues. A loop setting `approved=true` or
every check to `pass` is not a review. If the critic cannot be run independently,
report that concrete blocker before requesting approval.

Check source and candidate freshness before presenting. Once review passes,
show the target URL, account, response type, applicable rating and exact final
text returned by this command to the user:

```powershell
& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase show-response --run-id $RunId --post-id $PostId
```

Keep the returned token in the active approval context; do not copy it into
reports or handoff files. Ask once for approval of that exact response. No
personal name is needed. After the user explicitly approves it, execute:

```powershell
& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase authorize --run-id $RunId --post-id $PostId `
  --draft-hash $DraftHash --review-hash $ReviewHash `
  --presentation-hash $PresentationHash --approval-token $ApprovalToken

& $EngagePython .\engage_tiktok.py --database $Database `
  --master-database $MasterDatabase handoff --run-id $RunId --post-id $PostId

# Use the exact publication ID returned by handoff.
& $EngagePython .\publish_pending.py --database $Database `
  --master-database $MasterDatabase --publication-id $PublicationId --execute
```

Continue through handoff and execution after valid approval; do not stop at
`authorized`. The publisher revalidates the account, freshness, exact text,
native entity identity and duplicate history. It needs an enabled Submit
control and never uses Enter as a fallback. Confirm publication only from the
durable exact-text/target/account receipt with a remote comment ID. An uncertain
submission must be reconciled before retry; a screenshot failure must not cause
a confirmed comment to be posted again. Any later showcase post needs its own
separate approval.

## Request to give Google

```text
Prepare one LIVE comment on [SOURCE POST], with a connection to
[CANDIDATE POST/CREATOR] only if fresh evidence meets
the creator-matching rubric. Follow docs/contracts/ENGAGE_GOOGLE_PUBLISHING.md.
Refresh exactly those two known posts in a new ENGAGE refresh_known run in the
existing project database; preserve the earlier completed run and its artifacts.
Finish genuine analysis, native-label rehearsal, match import with --native-probe,
specific drafting and independent review. Show me the exact final comment for
approval without asking my name. After I approve that exact text, authorize it,
handoff and publish that one comment, then verify and report the receipt.
If human_verification_required occurs, preserve the verified Profile 7 tab and
wait for me to complete the CAPTCHA. Follow ENGAGE_PUBLICATION_RECOVERY.md for
worker interruptions; never overwrite queue history or blindly resubmit.
```
