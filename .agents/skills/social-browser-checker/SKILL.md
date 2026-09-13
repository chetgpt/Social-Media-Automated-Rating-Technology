---
name: social-browser-checker
description: >-
  Check browser login status or open social tabs in the shared Edge Profile 7,
  including YouTube, Instagram, Facebook, TikTok, X, Threads, and LinkedIn.
  Browser session checks do not verify official API access.
---

# Social Browser Checker

Use the workspace's `social_browser.py` with the required interpreter
`C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe`.
The only valid browser is the existing Microsoft Edge user-data root with
directory `Profile 7` in `existing_profile_attach` mode. Its visible label may
be `Profile 1`; verify the directory identity, not the label.

## Preserve an existing collection

Before a standalone check, respect any collection already using the shared
browser. For guarded MUSIC AUDIT, use that run's read-only `poll` first; do not
run low-level `social_browser.py status` or `start` alongside its operator or
collector. A fresh heartbeat, `RUNNING` task, held lock, live process, or uncertain
process inspection means wait. Silence or durable `collecting` alone is not a
browser failure.

- `RUNNING`: keep the exact task alive and wait.
- `INTERRUPTED` with no process or lock: preserve and ordinarily resume the same
  handoff directly, without preliminary browser diagnosis.
- `RESUME_READY`: if the user explicitly requested continuation, execute the
  returned `safe_same_handoff_action.argv` as the first browser-touching action.
  Otherwise preserve the run and report that continuation remains available.
- `COLLECTION_COMPLETE_NEEDS_FINALIZE`: use offline `finalize`.
- `COMPLETE`: stop workflow work or use offline `validate`.
- `BLOCKED`: preserve the run and report the blocker.

Do not create a replacement run or precede a guarded continuation with browser
start/status or `--after-restart`. Follow the complete guarded recovery contract
in the workspace [AGENTS.md](../../../AGENTS.md).

## Standalone login check or opening tabs

When no active collector owns the browser:

1. Inspect the designated session:

   ```powershell
   & 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe' social_browser.py status
   ```

2. If the connection is unreachable, attempt automatic Profile 7 startup/reuse:

   ```powershell
   & 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe' social_browser.py start
   ```

   When the user requested opening social tabs or signing in, add `--open-tabs`
   to `start`; this opens missing registered platform tabs in the same profile.
   It includes `https://www.threads.com/` and
   `https://www.linkedin.com/feed/`. Do not substitute another profile or the
   in-app browser. Report a startup or profile-verification failure without
   improvising a replacement launch path.

3. Require a reachable connection and verified Profile 7 identity before
   interpreting platform status. If the requested platform needs login, open
   its tab in that same profile and let the user complete login there. Recheck
   with the status command after login. Never ask the user merely to start Edge
   before attempting automatic startup/reuse.

The output checks scoped authentication-cookie names, including Threads
`sessionid` on `threads.com` or legacy `threads.net`, and LinkedIn `li_at` on
`linkedin.com`. Treat these as browser-session indicators: cookie presence does
not prove session validity, the active account, official API permissions, or
authorization to collect or publish. The separate Threads and LinkedIn API
workflows use their own access tokens and permission checks.

Only report platform names, login indicators, and cookie names/booleans. Never
print or persist cookie values, session tokens, authorization headers, or raw
browser credential payloads. Do not turn a login check into collection or an
outbound social action.

Keep shared Edge running. Never close it, use `social_browser.py stop`, kill Edge
or collector processes, delete browser-control state, create scheduled tasks,
or use headless/direct-port/replacement-user-data flags as a workaround.
