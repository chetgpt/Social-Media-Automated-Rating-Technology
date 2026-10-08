# Official Threads API setup

This guide applies to `threads_workflow.py` from any assistant with local shell
and file access, including Google/Gemini. The direct Meta adapter is implemented.
An app, authorized token, and live source validation are still needed before
claiming that a particular account can collect public Threads content.

As checked on 2026-09-19, no Meta per-request price or subscription tier was
published in the official setup documentation. This is not a guarantee about
future pricing. This integration adds no paid intermediary, hosting subscription,
or external AI service. Jev and other AI costs are separate.

## 1. Create the Meta app and add your account

1. Open [Meta for Developers](https://developers.facebook.com/apps/) and create
   an app with the **Threads use case**.
2. Use the **Threads App ID and Threads App Secret**, which differ from the
   regular Meta app credentials. Keep secrets outside the repository and chat.
3. In App roles, invite the intended account as a Threads Tester. Sign into that
   Threads account and accept the invitation under Website permissions.
4. For OAuth, configure the exact HTTPS redirect callback used by your client.
   Meta's [official sample](https://github.com/fbsamples/threads_api) documents
   local HTTPS callback setup. Its localhost restriction applies to the OAuth
   callback; collection itself can run on this workstation.

[Meta setup and tester instructions](https://developers.facebook.com/docs/threads/get-started/)

## 2. Obtain the token and appropriate access

Use Meta's app tools or [official Postman collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api)
to authorize the intended account and obtain a **Threads user access token**.
The runner accepts an already-issued token; it does not run an OAuth callback
server or store app secrets. You do not need to purchase a third-party API service.

Collection permissions:

| Purpose | Permission |
| --- | --- |
| All collection | `threads_basic` |
| Accessible replies/conversations | `threads_read_replies` |
| Topic search | `threads_keyword_search` |
| Public creator discovery | `threads_profile_discovery` |
| Optional own-account insights | `threads_manage_insights` |

Publication permissions are separate and are not needed to start read-only
collection. Existing exact-text approval and publication gates still apply.
Tester access and granted scopes do not establish public-discovery approval:

- Before approval for `threads_keyword_search`, search covers only the authorized
  user's own posts. Public search requires approval.
- Standard profile-discovery access is limited to selected Meta-owned accounts.
  Public-profile lookup only returns public accounts with at least 100 followers.
- A permission must pass App Review, and the app must be published, before users
  without an app role can grant that permission.
- A successful identity check or an empty search does not prove public access.

[Keyword restrictions](https://developers.facebook.com/documentation/threads/keyword-search),
[profile restrictions](https://developers.facebook.com/documentation/threads/threads-profiles).

For permission and expiry diagnostics, optionally provision a **Threads app
access token** through Meta's official authorization tools, and load it as
`THREADS_APP_ACCESS_TOKEN`. It is only used to inspect the user token at
`/debug_token`. It is not a replacement for the user token. Without it, the
diagnostic explicitly reports permissions as unverified.

## 3. Connect once on this Windows workstation

`threads_official_connect.py` accepts an already-issued Threads user token at a
hidden terminal prompt. It checks the exact account and makes a bounded own-post
probe before saving the token in **Windows Credential Manager**, scoped to this
Windows user and computer. It never saves app secrets or plaintext token files.
The credential name includes the supplied Threads app ID and normalized account;
this local naming convention alone does not verify the issuing app.

```powershell
$ThreadsPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$ThreadsAccount = 'YOUR_HANDLE'
$ThreadsAppId = 'YOUR_THREADS_APP_ID'
& $ThreadsPython .\threads_official_connect.py --app-id $ThreadsAppId --account $ThreadsAccount
& $ThreadsPython .\threads_official_run.py --app-id $ThreadsAppId --account $ThreadsAccount -- official-check --source own
```

Run these from the workspace. The connector also offers `--browser-input` for
assisted setup: a temporary, one-use form on `127.0.0.1`, with a random path and
short expiry, transfers the token directly to the local process. It is not an
OAuth callback, public website, or a way to sign into Meta. The process verifies
the account before saving. Close the form when it finishes; never copy the token
into a handoff, source file, command argument, or chat.

`threads_official_run.py` loads only the selected credential into the current
process environment, calls the existing runner, then restores the environment.
It supports `official-check`, `check-access`, and a new `collect`, enforcing the
exact account and official transport. It does not accept publication commands or
convert saved browser runs. For example, after resolving the user's exact source
and count:

```powershell
& $ThreadsPython .\threads_official_run.py --app-id $ThreadsAppId --account $ThreadsAccount -- collect --transport official --source topic --target $ThreadsTopic --posts $ThreadsPostCount --workflow listen --mode shadow
```

The store can be reused by Codex or Google/Gemini running as the same Windows
user. It adds no paid service. Token expiry and Meta's access restrictions still
apply; this launcher does not automatically refresh or replace the saved token.
Saved-run continuation remains on the canonical runner using the process
environment method below.

### Alternative: process environment only

Use PowerShell on the workstation. Replace only the account placeholder below.
Enter the token at the hidden prompt, not in chat, a command argument, `.env`,
source, or a saved transcript. The adapter reads only the process environment.

```powershell
$ThreadsWorkspace = 'D:\Kita Co. Lab\Music Audit Tool\Music Audit\TEST Tiktok Scraper Modules'
$ThreadsPython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$ThreadsRunner = Join-Path $ThreadsWorkspace 'threads_workflow.py'
$ThreadsAccount = 'YOUR_HANDLE'
Set-Location -LiteralPath $ThreadsWorkspace

# This command is offline and shows presence booleans, never credential values.
& $ThreadsPython $ThreadsRunner official-setup --source topic

$ThreadsSecureToken = Read-Host 'Threads user access token' -AsSecureString
$ThreadsTokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ThreadsSecureToken)
try {
    $env:THREADS_ACCESS_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ThreadsTokenPointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ThreadsTokenPointer)
    $ThreadsSecureToken.Dispose()
    Remove-Variable ThreadsSecureToken, ThreadsTokenPointer
}
& $ThreadsPython $ThreadsRunner official-check --account $ThreadsAccount --source own
```

To supply the optional debugger credential, repeat the hidden-prompt pattern
using `THREADS_APP_ACCESS_TOKEN`. Never print either environment value.
Environment changes in this terminal do not automatically reach another
already-running assistant or terminal. Run diagnostics and collection in the
same process environment; do not interpret an absent token as an API denial.

## 4. Check the requested source, then collect

`official-check` does not create or open a workflow database. Without `--probe`
it checks identity and, when configured, token validity, expiry and granted
scopes. With `--probe`, it additionally makes one source request for at most one
post and outputs only a diagnostic summary. It does not save post content or
test conversation completeness, deep pagination, publishing, or all permissions.

Examples below are command shapes, not authorization to run the example topic.
Set the exact scope and finite count from the user's request before collecting.

```powershell
$ThreadsTopic = 'USER_REQUESTED_TOPIC'
$ThreadsPostCount = 1
& $ThreadsPython $ThreadsRunner official-check --account $ThreadsAccount --source topic --target $ThreadsTopic --probe
& $ThreadsPython $ThreadsRunner collect --transport official --account $ThreadsAccount --source topic --target $ThreadsTopic --posts $ThreadsPostCount --workflow listen --mode shadow
```

Other sources: `--source own` without a target; `--source creator --target HANDLE`;
or `--source post --target NUMERIC_API_POST_ID --posts 1`. Official post IDs must
come from the API; browser shortcodes and converted browser IDs are not substitutes.
Use `--workflow audit` or `engage` only when that analysis or response work was
requested. `--workflow music-audit` remains metadata-only on Threads.

Save the run ID from the result. Continue only that run with `resume --run-id ID`.
The current browser run stays browser-backed. New official runs explicitly use
`--transport official`; changing an existing run's transport or falling back after
an API denial is unsupported. A custom global `--database PATH` must precede the
subcommand and be repeated for every operation on that run.

## Token refresh and quota handling

Short-lived tokens expire after one hour. Exchange them using Meta's official
tools for a long-lived token, valid for 60 days. An unexpired long-lived token
at least 24 hours old may be refreshed. The runner supports `--refresh-token`
on `official-check`, `check-access`, `collect`, `resume`, and evidence `refresh`.
The last four require a new or stored **Threads official** transport.

```powershell
& $ThreadsPython $ThreadsRunner official-check --account $ThreadsAccount --source own --refresh-token
```

Refresh keeps the returned token in memory for **that invocation only**. It does
not update the parent shell, the Windows credential store, or future scheduled
processes, and it never prints the token. To replace a saved credential, obtain a
valid long-lived token through Meta's tools and run the connection helper again.
Automatic renewal is not implemented. A failed refresh stops; it never falls
back to browser access or retries indefinitely.

Keyword searches have a 2,200-query rolling daily per-user limit shared across
apps; repeated nonempty searches count. A search page defaults to 25 results and
supports at most 100. Public-profile lookup has a separate 1,000-request daily
limit. General call and processing quotas also apply. These limits do not promise
complete historical coverage. Token and permission failures are reported separately
from rate limits; a blocked response does not trigger another transport.

[Token lifecycle](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens/),
[API quotas](https://developers.facebook.com/docs/threads/overview/).

When finished with the terminal session:

```powershell
Remove-Item Env:THREADS_ACCESS_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:THREADS_APP_ACCESS_TOKEN -ErrorAction SilentlyContinue
```

## Completion and model handoff

Google/Gemini runs the same commands with the required local Python interpreter.
No Google API key is needed. Preserve platform, database, run ID, transport, account,
source and collection status in the handoff. Never copy tokens into the handoff.
Report missing setup, unknown permission status and collection shortfalls honestly.
Fixture tests establish local behavior; live readiness requires an authorized
account plus successful checks for the exact requested source.
