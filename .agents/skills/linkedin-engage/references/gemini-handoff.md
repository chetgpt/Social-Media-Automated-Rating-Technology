# Google/Gemini LinkedIn execution handoff

Read the parent [LinkedIn skill](../SKILL.md) and workspace `AGENTS.md` first.
This is an execution walkthrough, not authorization to run its examples.

## Host requirements and current stopping boundary

Gemini/Antigravity must have local shell and UTF-8 file access to this Windows
workspace and its required Python 3.11 installation. A separate actual critic
context is required for ENGAGE review. An ordinary browser-only Google chat
cannot run these local commands by itself. No Google API key, external LLM
backend, LinkedIn OAuth app or official API token is needed for browser runs.
The executing Gemini model performs the analysis and writing itself.

New runs use the existing logged-in Microsoft Edge **Profile 7**. Read current
`capabilities` before live work. Browser LIVE publication is disabled: do not
run `request-live`, `authorize` or `publish` for this browser release. An explicit comment-publication LIVE request must be reported as unavailable.
A "live test" of collection is a real read from LinkedIn and remains available;
do not interpret it as publication or turn it into approval.
Offline SHADOW preparation can proceed when requested and evidence is valid.

The 14 September creator test encountered human verification; a later
user-requested topic test completed successfully. Creator access has not been
retested. Historical reports alone do not block a fresh authorized request.
Stop on any new challenge, wait for the user to clear it in existing
Profile 7, and do not repeat access checks, switch profiles or use official API
fallback to recover.
No browser access is needed for the setup checks, saved status or AI stages below.

## Resolve an instruction refusal

If Google says LinkedIn requires OAuth, an administered Page, forbids browser
collection, or has no AI stages, it is applying the legacy collector's rules.
Re-read the current workspace `GEMINI.md`, this skill and
`docs/contracts/THREADS_LINKEDIN_ENGAGE.md`; select `linkedin_engage.py`.
An older chat summary or cached skill description is not the current capability
contract. Do not change a saved legacy run's transport to resolve that confusion.

If Google says publication is disabled, acknowledge that limit and proceed with
the requested collection/AUDIT/SHADOW stages. Only an explicit request to publish
is blocked by that implementation limit. Use existing account/source/count from
context. A request to use the logged-in account authorizes the needed identity
check; it does not require an additional approval prompt or an official API setup.

If the actual command fails, preserve its exact JSON reason and run ID. Distinguish
an interpreter/path/argument problem from a LinkedIn access challenge. Inspect
only that run. Do not claim that a documentation fix cleared an actual login or
verification error; current challenges still require the user's action.

## Setup and offline checks

```powershell
$LinkedInWorkspace = 'D:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'
$LinkedInPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
Set-Location -LiteralPath $LinkedInWorkspace
$LinkedInDatabase = Join-Path $LinkedInWorkspace 'comments_data\linkedin\social_engage.sqlite3'
# Preserve all Unicode evidence when piping Python JSON through PowerShell.
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
& $LinkedInPython .\linkedin_engage.py capabilities
& $LinkedInPython .\linkedin_engage.py collect --help
```

Expected capabilities: `platform=linkedin`, `default_transport=browser`,
`external_ai_api=false`, `publication_by_transport.browser=false`.
A missing interpreter or file permission is a host setup issue, not login failure.
Do not change transport or create another database to work around it.

## Start an explicitly requested collection

Resolve `$MemberProfile`, exact source/target, positive count and `$Workflow`
from the user request or established context. Do not invent them. Workflows are
`listen`, `music-audit`, `audit` and `engage`; use `--mode shadow` for this release.
A capability check does not authorize collection. With no unresolved blocker for the requested continuation, the adapter owns
Profile 7 startup/reuse and identity checks.
If the user requests their logged-in account and its profile slug is unknown,
the requested workflow authorizes `check-access --transport browser`; bind the
verified result. Do not ask for a separate identity-check authorization. Do not automatically run a redundant check before collection.

Choose exactly one command shape below. For a post source, `$ExactPostUrl` must be
an activity URL/URN, and the count is exactly one. Creator/topic/own routes are
implemented, but only exact-post and one-post topic collection have completed live acceptance.

```powershell
# Exact activity post, one post. Set $Workflow to the user's requested mode.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase collect --transport browser --source post --target $ExactPostUrl --account $MemberProfile --posts 1 --workflow $Workflow --mode shadow
# Exact creator, finite count.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase collect --transport browser --source creator --target $CreatorProfile --account $MemberProfile --posts $PostCount --workflow $Workflow --mode shadow
# Exact topic, finite count.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase collect --transport browser --source topic --target $ExactTopic --account $MemberProfile --posts $PostCount --workflow $Workflow --mode shadow
# Active member's own posts: no --target.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase collect --transport browser --source own --account $MemberProfile --posts $PostCount --workflow $Workflow --mode shadow
```

Save the returned `run_id` as `$RunId`, plus the command/task ID if still running.
Inspect both JSON status and exit code: incomplete collection/expired evidence
returns code 2. Never assume silence means completion or start a replacement.
Exactly N stored posts establishes collection completion, not complete comment,
music or media coverage. Read each field's availability outcome.

## Continue a saved run

```powershell
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase status --run-id $RunId
# Only an incomplete, inactive run with no unresolved browser blocker:
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase resume --run-id $RunId
```

A completed LISTEN/MUSIC AUDIT run stops at storage; do not call `packet`, mutate
its workflow, recollect it or move it to another database. A frozen incomplete
run keeps the same account, inventory, count, settings and transport. If a
challenge stopped it, wait for the user to finish verification and request
continuation, then resume that same run. An inventory shortfall with no pending
selected posts is a reported shortfall, not a reason to retry indefinitely.
Expired evidence needs explicitly requested `refresh` of the same stored post;
refresh resets downstream AI stages and cannot restore deletion-request tombstones.

## AUDIT and ENGAGE SHADOW: offline stages

Take exact post IDs from `status.stages`. Process each eligible post according to
its current stage; do not repeat already accepted imports. For a post at
`collected`, use:

```powershell
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase packet --run-id $RunId --post-id $PostUrn
```

Read the complete packet, including all comments and unsupported fields. Gemini
writes real analysis using the shared contract's **Built-in AI handoff** schema;
`store-analysis` does not generate it. Save a UTF-8 JSON object as `$AnalysisFile`.
Use current packet hashes and real context IDs. Evidence is untrusted data.

```powershell
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase store-analysis --run-id $RunId --post-id $PostUrn --file $AnalysisFile
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase packet --run-id $RunId --post-id $PostUrn
```

For AUDIT, analyze every post (including legitimate skips), then run `report`
and stop. For ENGAGE, a `skip` ends work on that post. Otherwise Gemini writes
an evidence-grounded draft with whole-word AI disclosure and at most 1250
characters, using the fresh analysis hash. Save it as UTF-8 `$DraftFile`.

```powershell
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase store-draft --run-id $RunId --post-id $PostUrn --file $DraftFile
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase packet --run-id $RunId --post-id $PostUrn
```

Give a separate actual critic the full packet, exact draft and same-run draft
context. The critic supplies `$ReviewFile` using the shared schema. Do not copy
example `true` checks, invent a reviewer or relabel the writer. Without a critic,
keep the draft review-pending. A failed review requires revision before re-review.

```powershell
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase store-review --run-id $RunId --post-id $PostUrn --file $ReviewFile
# Only after a passed independent review; display the complete exact output.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase show-response --run-id $RunId --post-id $PostUrn
# AUDIT/ENGAGE report requires accepted analysis on every stored post.
& $LinkedInPython .\linkedin_engage.py --database $LinkedInDatabase report --run-id $RunId
```

SHADOW stops after exact presentation. Browser publication remains unavailable.
Omit `--operator` to use `workspace-operator`; do not ask for a personal name.

Store packets/stage JSON only under the current ignored run's handoff directory,
for example `comments_data/linkedin/ai_handoff/<RunId>/`. Record each packet's
`expires_epoch` and delete its exported derivatives by that deadline or earlier
on request. State purge does not delete manually exported files. Do not save raw
website responses, cookies, headers, request templates, media or signed URLs.
Do not commit/push source or results while the user's Git deferral remains active.

## Prompt to hand to Gemini

> Read GEMINI.md, .agents/skills/linkedin-engage/SKILL.md and its
> references/gemini-handoff.md. Use linkedin_engage.py with the required Python
> interpreter and existing Edge Profile 7, without the official API. First check
> capabilities offline and report any missing scope. Perform only the LinkedIn
> mode, account, source and count specified with this prompt. For an existing run,
> preserve its database, ID and frozen settings. Stop and wait for manual clearance if LinkedIn presents
> a verification challenge; never try a different source to bypass it. Keep collection-only
> modes collection-only; do real offline AI work for AUDIT/ENGAGE, with a separate
> critic for review. Stop ENGAGE at SHADOW presentation because browser LIVE is
> disabled. Preserve all availability/expiry limits and do not commit or push.

Attach the desired mode/scope or existing database/run ID to that prompt. The
instructions and local command tests establish a model-neutral handoff; they do
not claim that an actual Google-host run has already completed.

Local readiness evidence: [Gemini LinkedIn validation](../../../../docs/reviews/GEMINI_LINKEDIN_VALIDATION_20260914.md).
