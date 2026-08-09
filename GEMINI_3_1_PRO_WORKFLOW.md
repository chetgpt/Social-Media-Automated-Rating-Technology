# Gemini 3.1 Pro TikTok Workflow Adapter

**Adapter version:** 4.0.0

**Applies to:** Gemini 3.1 Pro / Antigravity agentic execution in this workspace

**Active workflows:** TikTok `LISTEN`, `AUDIT`, and `ENGAGE`

This document is a model-specific execution adapter. It is deliberately short
and does not redefine the workspace workflow.

## 1. Authority and conflict resolution

Use this authority order on every task:

1. `AGENTS.md` (binding workspace contract)
2. `WORKFLOWS.md` (current operating guide)
3. The actual command's current `--help` output and database schema
4. This Gemini adapter

If this document conflicts with a higher authority, ignore this document and
follow the higher authority. Do not rely on remembered commands or older chat
examples when current help differs.

The following legacy behaviors are prohibited:

- automated or zero-touch presentation, authorization, handoff, or publication;
- `auto_authorize_pipeline.py` as an authorization substitute;
- Edge Default, directory `Profile 1`, managed/temporary profiles, Playwright
  profiles, or the in-app browser;
- a configurable or bare Python command on this workstation;
- legacy `--count`, `--input`, or `--output` ENGAGE examples;
- direct SQLite writes, fabricated hashes, placeholder evidence, or bypassed
  workflow gates; and
- use of `incremental_project.py` or `run_scraper.py` for an active workflow.

## 2. Fixed workstation bootstrap

At the beginning of every new task, use exactly:

```powershell
$Workspace = 'D:\Kita Co. Lab\MAIN Backup for Antigravity\TEST Tiktok Scraper Modules'
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
Set-Location -LiteralPath $Workspace
```

Never use bare `python`, `py -3`, another Python installation, or another
working directory for workspace commands.

Read `AGENTS.md` and `WORKFLOWS.md` completely before taking workflow action.
Check the relevant command's `--help` before using a command shape that is not
already established by the current task.

## 3. New-project state contract

A new project creates a new local workflow scope; it does **not** create a new
browser identity. Derive a filesystem-safe project slug and use:

```text
comments_data/project_<slug>/state/engage_state.sqlite
```

Every production collection also attaches the workspace-global master database:

```text
comments_data/tiktok_master/state/tiktok_master.sqlite
```

Never delete, reset, replace, copy over, or edit either database directly.
Before assuming a project database is empty, determine whether it already has
an unfinished run for the same immutable request. Resume that run rather than
creating a duplicate. A blocked command is not permission to create a new run.

If a failed collection response does not expose its run ID, recover the latest
matching run through a canonical read-only status/list facility when available.
If no such facility is available, a read-only SQLite URI (`mode=ro` plus
`PRAGMA query_only=ON`) may be used only to identify the run and counters. Never
write workflow state with raw SQL. Record this recovery as a tooling deviation.

## 4. Mandatory Profile 7 deployment

The only permitted social browser is Microsoft Edge's real user-data root,
directory `Profile 7`, in `existing_profile_attach` mode. Its visible Edge
label may be `Profile 1`; the directory name is the canonical identity.

The first state-changing workflow command must be the applicable
`engage_tiktok.py collect` or `resume-collect`. That command owns automatic
Profile 7 startup or reuse. Do not ask the user to start Edge first. Do not run
a separate low-level browser startup before every project.

Low-level commands are diagnostic only:

```powershell
& $EngagePython .\social_browser.py start
& $EngagePython .\social_browser.py status
```

Do not use `social_browser.py --open-tabs` for this TikTok-only workspace; the
current option may open tabs for other platforms.

### Browser origin and per-command disposition

Keep these two facts separate:

- `bridge_origin` is the historical origin persisted by the current launcher:
  `enabled_here` or `adopted_existing`;
- `invocation_disposition` describes this specific command: `launched`,
  `reused`, `adopted`, or `unknown`.

The current tooling may expose only the historical `bridge_origin`. It does not
always attest whether this specific invocation reused a saved endpoint. When
that fact is unavailable, record `invocation_disposition=unknown` plus a
tooling-observability deviation. Never derive the per-command disposition from
the historical origin, and never say "launched" or "started a new browser"
unless the current invocation independently proves it.

Before TikTok discovery or refresh, the preflight must establish all of these:

1. the local connection is reachable;
2. the attached directory is exactly `Profile 7`;
3. mode is `existing_profile_attach`;
4. TikTok authentication is present;
5. the active TikTok handle resolves; and
6. when an expected account was supplied, the observed handle matches exactly.

The collector should open or reuse exactly one visible TikTok home tab in
Profile 7, perform a bounded page-readiness/reload retry, and keep Profile 7
running. It must never close the browser or user-owned tabs. If the current
tool closes the only useful TikTok tab or cannot expose a visible intervention
tab, report `tooling_noncompliance` rather than pretending that the browser was
visibly deployed.

If the handle still cannot resolve after the automatic retry:

- preserve the existing run and database;
- leave a useful TikTok tab open when the tool supports it;
- report the real run ID, absolute database path, durable status and counters,
  available bridge origin, per-command disposition or `unknown`, browser-check
  identifier, and sanitized error;
- stop before all AI stages;
- ask for human browser interaction only at this point; and
- resume the same run after correction.

Never fall back to another browser/profile, lower the requested count, or create
a replacement run.

Profile 7 is accessed only for collection/refresh and immediately before an
authorized outbound TikTok action. Analysis, AUDIT aggregation, drafting,
review, storage, presentation, and authorization use local stored state and
must not start, attach to, or revalidate the browser.

## 5. Workflow routing and stopping boundaries

Parse the user's shortcut according to `AGENTS.md`. When ambiguous, choose
`ENGAGE SHADOW` and perform no outbound action.

### LISTEN

```text
browser preflight -> exact collection -> evidence storage -> stop
```

LISTEN cannot export or import AI analysis and cannot draft or publish.

### AUDIT

```text
browser preflight -> exact collection -> built-in AI analysis of every post
-> deterministic hash-bound report -> audit_complete -> stop
```

AUDIT cannot reclassify responses, draft, review responses, present, authorize,
handoff, or publish.

### ENGAGE SHADOW

```text
browser preflight -> exact collection -> built-in AI analysis
-> response classification -> built-in AI drafting
-> independent built-in AI critic -> reviewed response storage -> stop
```

SHADOW cannot present, authorize, hand off, or publish.

### ENGAGE LIVE

Live intent does not authorize publication. The exact stored response must be
shown to a named non-AI human with `show-response`. That same human must approve
the exact presentation-bound text using the one-time token. Only then may the
normal publication preflight, handoff, sequential publication, receipt, and
separate COMMENT SHOWCASE process occur. Never infer approval and never expose
an approval token in an attempt log.

## 6. Current command patterns

Use current CLI help as the final authority. These are shape examples, not
hardcoded project values.

### New topic collection

```powershell
& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  collect `
  --project $Project `
  --topic $Topic `
  --posts $RequestedCount `
  --workflow engage `
  --collection-policy new_only `
  --mode shadow
```

Use `--workflow listen` for LISTEN and `--workflow audit` for AUDIT. LISTEN and
AUDIT are always shadow. Creator collection uses `--creator` instead of
`--topic`; creator `ALL` uses `--all-posts` instead of `--posts`.

### Resume and status

```powershell
& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  resume-collect --run-id $RunId

& $EngagePython .\engage_tiktok.py `
  --database $Database `
  --master-database $MasterDatabase `
  status --run-id $RunId
```

Resume reuses the saved immutable request, browser/account binding, collection
policy, refresh selection, and master database. Do not restate different run
settings on resume.

### Built-in AI queue operations

Use only canonical queue export/import commands with `--run-id` and `--file`.
Every import also supplies the required `--actor`. Do not use `--input` or
`--output`.

Recommended Gemini actor identifiers:

```text
antigravity-gemini31-analysis
antigravity-gemini31-drafter
antigravity-gemini31-critic
```

The critic must operate in a genuinely separate context and its actor must
differ from the drafter. If independent review is unavailable, stop rather
than self-approve. Built-in Gemini/Antigravity performs semantic work from the
stored queue; no external LLM API or placeholder-generating script may replace
it. Tool-owned hashes and canonical imports remain authoritative.

Do not advance when collection is `X/N`. Exact `N/N` evidence-ready records are
required. For a creator `ALL` run, the verified terminal inventory determines
the frozen requested count.

## 7. Observable attempt ledger

For model evaluation, create a sanitized attempt directory under:

```text
comments_data/model_attempts/<UTC_TIMESTAMP>_gemini31/
```

The model-authored diary is secondary evidence. The workflow database, master
database, canonical artifacts, events, browser checks, and publication receipts
remain authoritative.

Append one JSONL record before and after every command/tool action. Use a
strictly increasing sequence beginning at `1`. Record observable facts, not
private chain-of-thought:

- UTC timestamp, stage, action, and concise policy basis;
- exact sanitized absolute interpreter, working directory, and argv;
- command start/end, exit code, duration, and sanitized error;
- real run ID, post ID where applicable, database path, and event/check IDs;
- status and counters before and after the action;
- available bridge origin, per-command disposition or `unknown`, and resolved
  account result;
- input/output artifact paths, record counts, and SHA-256 hashes;
- command-level retries separately from internal browser/page retries;
- deviations, blockers, and the next permitted stage; and
- publication/navigation actions attempted and completed as explicit lists.

Never record cookies, authorization headers, CDP/WebSocket URLs or UUIDs,
access tokens, approval tokens, signed media URLs, `msToken`, signatures, or
environment secret values. Use `<redacted>` when a field cannot safely be
omitted.

After every durable stage, run the canonical status command and record the
authoritative returned state. Do not claim success from narrative output alone.
On failure, include the failure itself in the ledger and preserve all durable
state.

The final summary must include:

```text
executor_compliance: PASS | FAIL
task_outcome: COMPLETE | BLOCKED
run_id
absolute database path
durable status and all counters
bridge origin, per-command disposition or unknown, and account verification
result
artifact manifest with hashes
deviations and retries
outbound actions attempted/completed
final stopping reason
```

Model/provider/version identity in this summary is self-claimed unless the
runtime supplies an independent attestation.

## 8. Completion checklist

Before declaring a task complete, verify:

- the required Python interpreter and workspace were used;
- only verified Edge directory `Profile 7` was used;
- bridge origin and per-command disposition were reported without inference;
- the local and master databases contain the same run and counters;
- exact collection reached its immutable requested count;
- no AI stage touched Profile 7;
- LISTEN, AUDIT, and SHADOW stopped at their required boundaries;
- ENGAGE drafting and review actors were independent;
- no direct SQL writes or fabricated hashes occurred;
- no presentation, authorization, handoff, publication, or showcase action was
  inferred; and
- no secret appeared in logs or chat.

A platform or browser blockage may produce compliant execution with a blocked
task outcome. Never claim completion without the authoritative artifacts.
