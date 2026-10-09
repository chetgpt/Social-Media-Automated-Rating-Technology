# Optional REA browser probe

This isolated diagnostic observes one already-open Threads, LinkedIn, or TikTok
tab in the existing verified Edge Profile 7. It is not imported by LISTEN,
MUSIC AUDIT, or any collector. It does not change their browser configuration,
launcher, dependencies, or saved browser state.

Use the required workspace Python and the separately installed REA 6.0.0 package:

```powershell
& 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe' tools/rea_browser_probe.py --platform threads
```

The default package location is `%TEMP%/codex-rea-trial-6.0.0/node_modules/rea-agents`.
`--rea-package` selects another installation of that exact version. REA and its
`ws` dependency remain outside this project's Python requirements. Node.js must
be on PATH and satisfy REA's Node engine requirement.

The wrapper refuses to run alongside detected collection processes or without
the existing verified Profile 7 connection. Live profile verification uses the
existing helper's temporary `edge://version` tab, closing only that owned tab.
It never starts/restarts Edge or rewrites browser configuration. Do not start a
collection during this short diagnostic; process checks are not an atomic lock.

A temporary loopback HTTP/WebSocket bridge supplies REA's required discovery
protocol over the existing browser connection. It exposes one selected target
and permits only inspection commands plus attachment/detachment of its own CDP
session. Navigation, input, request replay, response-body/script-source retrieval,
credential reads, and browser/tab closure are not allowed. The bridge closes its
own sockets when finished, leaving Edge and its tabs running.

Observation lasts 500 ms, with a 30-second sidecar deadline, 4 MiB inbound CDP
budget, and 2,500-message limit. Crossing a limit stops the probe; it is not a
complete capture. The Python wrapper also bounds its child process. No raw
capture is written to disk or emitted to chat. Only structural counts and
aggregated known API routes are emitted; URL queries, fragments, arbitrary path
values, titles, text, headers, and session identifiers are omitted. Raw protocol
messages are transiently processed in memory.

The summary reports only activity after attachment. Zero observed requests is
possible on an idle page and does not prove that an API is unavailable. Worker
visibility is deliberately excluded by the single-target bridge. This probe
does not measure scraping throughput or authorize collection/publication.

If the result is blocked, preserve the existing workflow/browser state and
address the reported reason; do not substitute another profile. REA upgrades
require reviewing the inspection command contract before changing the version
pin or allowlist.

## Threads pagination follow-up

The separate Threads browser adapter now recognizes post-page `data.media`
fragments and their direct-reply connections. It joins fragments only when
validated post and author identities agree. A later reply page may use the
current navigation's validated root only when the observed query targets that
exact post. Missing visibility evidence still prevents a completeness claim.

Replayed read queries drain and discard their response body with a 12-second
abort and 8 MB limit, then wait for that exact response to be parsed. They no
longer use the fixed 1.5-second settle delay or wait for unrelated responses.
Native navigation/scroll settling is unchanged. Requests, credentials, root
hints and raw responses remain in memory; the REA probe is not a collector
dependency. Existing Profile 7 configuration and TikTok workflows are unchanged.

The comment limit counts verified direct and nested replies together, avoiding
another page request when the requested sample is already loaded. Post
pagination follows the exact root's outer reply connection; its latest state
controls terminality. Nested page markers remain separate. The observed
`has_unavailable_replies` declaration supplies root visibility within the same
navigation, while missing declarations remain unverified and observed hidden
replies prevent completeness.

`comments_coverage` reports bounded counts and explicit incomplete reasons,
including the comment limit, unfinished outer/nested pages, and count or
visibility uncertainty. It contains no raw request data or cursors.

After outer pagination, the adapter can continue replies beneath already
verified conversation members. It requires the observed
`BarcelonaPostPageDirectRepliesRefetchQuery` template, changes only its post ID
and cursor, and validates the exact child response before adding replies.
Child parent links are preserved under the original conversation root; child
terminal markers cannot terminate outer pagination. Missing visibility or
reply-count discrepancies still prevent a completeness claim.

Nested reads stop at the shared comment limit, repeated/missing cursors,
unverified responses, platform denials, or 40 requests. A child without a
validated raw post or an observed query template remains incomplete. Raw child
metadata and request templates are cleared when navigating or closing the
adapter. This improves observed reply coverage; it does not establish an
exhaustive platform search or a general throughput benchmark.
