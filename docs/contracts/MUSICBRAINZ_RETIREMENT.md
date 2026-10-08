# MusicBrainz retirement

MusicBrainz is retired from MUSIC AUDIT and its collection/enrichment entry
points. No future MusicBrainz request is permitted, including retries, backfill,
resumes, or direct adapter use. This change does not add a replacement provider.

## Current collection

New runs default to an empty configured catalog-provider set. Do not add
`musicbrainz` to a new run. MUSIC AUDIT still collects TikTok's declared music,
structured contained-recording metadata, and eligible exact-ID Apple lookup
results through the existing `apple_itunes_lookup` resolver. Its provenance,
sanitization, pacing, and terminal-outcome rules remain unchanged. A successful
Apple result does not trigger a MusicBrainz query.

Music identity remains metadata evidence. A catalog-correlated identity is not
acoustic verification, and a generic original-sound label does not prove who
created the audible recording. Media acquisition, AI, and publication boundaries
are unchanged.

## Preserved runs and historical evidence

Do not remove MusicBrainz from a frozen run, handoff, or backfill manifest.
Existing evidence records, catalog results/candidates, provenance, exports, and
hashes remain unchanged. Reading, validating, or exporting those records must
not contact MusicBrainz or rewrite them to the new defaults.

If an unfinished preserved run still has `musicbrainz` in its frozen provider
set, a newly collected or repaired record receives a terminal MusicBrainz
`unsupported` outcome with reason `provider_retired`, without a network request.
This allows the same immutable run to continue under the normal exact-count,
evidence-ready, source, browser, and resume gates. It does not permit changing
an already evidence-ready record or substituting another run.

Retirement is not `not_found`, a match, a provider outage, a retry, or a cache hit.
It consumes no MusicBrainz request slot. Historical `matched`, `ambiguous`,
`not_found`, `unsupported`, `unavailable`, `rate_limited`, and `provider_error`
results remain readable with their original meaning and hashes. Retained
MusicBrainz field names and compatibility code do not authorize future requests.
