# ENGAGE live test and single-operator approval

The user requested a real publication test and removal of personal-name entry
on 7 September 2026. The sole user is recorded internally as
`workspace-operator`. Independent review and explicit approval of the exact
rendered comment remain required. This document records observed progress;
publication must not be claimed without a confirmed receipt.

## Approval change

`engage_tiktok.py show-response` and `authorize` no longer require
`--presented-to` or `--authorized-by`. Omitted values use `workspace-operator`.
The label represents the user's single-operator preference; it is not an
invented personal name or an approval signal. Existing explicitly named
presentations retain their exact identity binding.

The normal sequence is now:

```text
independent content review -> stored exact comment -> show-response
-> user approves the shown text (no name question) -> authorize with bound token
-> handoff and publication checks -> one publish -> verify and store receipt
```

No review, hash, token, identity-match, freshness, duplicate or LIVE-mode check
was removed. The name-free change and related existing paths passed 86 tests
in 24.05 seconds, including successful publisher handoff with the default
identity and rejection of wrong/reused tokens, wrong legacy identity,
unreviewed content, SHADOW authorization and stale handoff.

Read [the Google workflow](../../GEMINI_3_1_PRO_WORKFLOW.md), now version 7.5.0.
Any older "named human" wording denotes this same user, not a name-entry step.
Separate showcase-post approval remains separate; where its CLI still requires
identity flags, use the same audit label without asking for a personal name.

## Selected live test

The previous five-post run is immutable SHADOW state and its source evidence
had expired under the default 60-minute publication window. No supported CLI
converts that stored run to LIVE. The user-requested live test therefore uses
an explicit refresh of only the source and its matched candidate:

- Source: [Piano Soin, Van Gogh](https://www.tiktok.com/@pianosoin/video/7451330450527800598).
- Candidate: [Andrew Piano, Fur Elise](https://www.tiktok.com/@andrew.piano/video/7559637866326936846).
- Topic: `piano tutorial`, fixed count 2, `refresh_known`, `mode=live`.
- Account: `kitascore`, existing Edge Profile 7.
- Run: `engage_109416138ac74ef6`.
- Project: `comments_data/project_engage_live_piano_pair_20260907_1305`.

The intended first publication is one reviewed Piano Soin comment mentioning
Andrew. Collecting and analyzing the candidate does not approve a second
comment. The global publication guard initially reported no confirmed or
uncertain comment on either target for kitascore; it must be checked again at
publication time.

```powershell
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$Database = 'comments_data/project_engage_live_piano_pair_20260907_1305/state/engage_state.sqlite'
$RunId = 'engage_109416138ac74ef6'
# The run already exists: do not repeat collect or create a replacement.
& $EngagePython .\engage_tiktok.py --database $Database `
  resume-collect --run-id $RunId
```

## Refresh observations and repair

Initial Profile 7 preflight passed in 36.766 seconds. The first refresh ended
after 15.094 seconds with 0/2 evidence-ready posts: HTTP metadata hydration had
failed, so captions, comments and transcripts were not counted as fresh.
The same run was preserved. A later same-run continuation encountered the same
failure.

Bounded diagnostics found HTTP 200 responses containing only 1,462 characters
and no exact-post metadata. Ordinary verified browser navigation later returned
more than 475,000 characters including the exact source item. No raw HTML,
credentials or media were saved as evidence by these diagnostics. HTTP 200
alone is not collection success.

`refresh_video_candidates_from_html` now tries one ordinary browser navigation
when a successful direct HTTP response lacks the requested item. It uses a
temporary tab in the already verified Profile 7 context, blocks media requests,
and reads the same rehydration parser. It enforces the same canonical post path
and exact owner, never searches or substitutes another post, and closes only
its tab. Non-success HTTP responses do not trigger this fallback. Missing data,
redirects, owner mismatch and timeouts still fail; cached values never fill the
freshness gate. Successful fallback provenance is `tiktok_browser_rehydration`.

The fallback and related refresh/profile tests passed 23 tests in 3.03 seconds.
It has no challenge interaction, alternate profile, proxy or access bypass.

The combined approval, refresh, canonical-stage, matching and publisher tests
then passed **191 tests in 53.03 seconds**. A live invocation of the actual
metadata function succeeded for both exact posts: Piano Soin via direct HTML
in 3.829 seconds and Andrew via browser rehydration in 20.359 seconds. The
diagnostic invocation took 66.375 seconds including preflight. These metadata
checks alone are not evidence-ready collection checkpoints and cannot replace
the required full comments/subtitle refresh. The canonical same-run collection
was continued with process-local `TIKTOK_METADATA_CONCURRENCY=1`; the selected
IDs, count, freshness rules and source scope were unchanged.

## Final status of this preparation attempt

**Blocked before analysis and publication: 0/2 evidence-ready, 0 authorized,
0 published.** The last canonical continuation still failed to refresh both
records. A subsequent initialized-collector diagnostic refreshed Andrew but
could not obtain Piano Soin's exact metadata within its bounded browser wait.
The successful earlier isolated checks therefore did not establish reliable
current availability. No captcha, account logout or wrong-account evidence was
observed, and no browser/account recovery was requested from the user.

The old SHADOW evidence and exact two-ID LIVE refresh remain preserved. No
personal name was requested, no presentation token was issued, no approval was
recorded, and no publication attempt was made. Reusing old hashes/timestamps,
extending the publication window, substituting a different post, or editing
the SHADOW mode would not be valid recovery. All collector/diagnostic processes
from this preparation have ended; the shared Profile 7 session was left open.

The completed application change is name-free approval. The browser fallback
passed its unit checks and one actual exact-post read, but reliable completion
of this live pair and real server-side publication are **not verified**.
Error logging now emits a bounded phase/reason such as `direct_request`,
`browser_navigation`, `browser_item_unavailable` or a network error code, while
omitting transport exception strings that may contain credentials or signed URLs.

The canonical attempt ledger records:

| Attempt | Profile 7 preflight | Collection after preflight | Evidence-ready |
|---|---:|---:|---:|
| Initial | 36.766s | 15.094s | 0/2 |
| Same-run continuation 1 | 51.203s | 19.031s | 0/2 |
| Same-run continuation 2, browser fallback | 46.031s | 15.125s | 0/2 |
| Same-run continuation 3, sequential metadata | 43.594s | 30.375s | 0/2 |

The full authoritative timings are retained in
[the final status receipt](../../comments_data/project_engage_live_piano_pair_20260907_1305/live_preparation_status.json).
Diagnostic reads are separate from these collection times and do not create
evidence-ready checkpoints. Tests, code work and interaction pauses are not
included in the operation times above.

On a later user-directed continuation, inspect this same run and use its
`resume-collect` command. Resume alone may not resolve the external availability
problem; diagnose a concrete new error before repeating attempts. Once it
reaches 2/2, read the fresh full evidence, reanalyze both posts, import same-run
matching with observed native labels, draft and independently review the source
comment, and show its exact rendered text. Only then request the user's final
approval, without asking a name. After actual publication, document the remote
comment ID, exact text/target/account confirmation, visible native mention
verification, timing and screenshot outcome. A composer entity or plain-text
creation response alone must not be reported as notification delivery.
