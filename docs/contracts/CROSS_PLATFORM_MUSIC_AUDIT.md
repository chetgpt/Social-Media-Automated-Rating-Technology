# Cross-Platform Music Audit Design

## Status and scope

This document defines the staged, metadata-only `LISTEN` design for Instagram,
Facebook, X, and YouTube. The offline evidence contract, projection, durable state,
and import/export CLI are implemented. This does not activate a live platform
adapter, add a chat shortcut, or change the workspace's canonical TikTok-only
execution contract. Platform adapters, permissions, retention handling, and
adapter-specific tests must exist before any platform may claim a live durable
collection run.

The proposed workflow collects attributable platform metadata and records explicit
terminal outcomes. It stops at durable evidence storage. It does not analyze the
meaning of a post, evaluate music-to-content fit, draft a response, engage with a
user, or publish anything.

## Non-negotiable metadata-only boundary

Cross-platform collection must never:

- download, cache, or persist audio or video bytes;
- extract an audio stream, fingerprint audio, or perform acoustic recognition;
- run speech-to-text or infer a transcript from media;
- scrape, reconstruct, reproduce, or infer lyrics;
- infer a song identity from a caption, hashtag, filename, category, waveform, or
  surrounding text;
- treat a platform's `original sound`, category, copyright flag, topic, tag, or
  recommendation as proof of recording identity or ownership;
- persist cookies, authorization headers, access tokens, signed media URLs,
  playback URLs, caption-download URLs, raw private API payloads, or other
  transport credentials; or
- use undocumented endpoints in an adapter represented as an official API
  adapter.

The offline importer retains only its closed projection of caller-supplied
metadata. A future live workflow may retain only metadata returned through its
frozen adapter. Missing fields are never synthesized.

## Delivery stages

### Stage 0 (production target): Freeze authority and capabilities

The implemented manifest is deliberately limited to the offline importer. It
hash-binds the platform, importer version, `offline_normalized_import` authority,
caller-supplied-record access scope, accepted projection fields, source-mode
labels, and storage limits. It does **not** authenticate the caller's claimed
field provenance or represent an official live platform authorization.

Before a live adapter can be enabled, its expanded versioned manifest must identify:

- the platform and adapter version;
- whether the adapter is `official_api` or `experimental_authenticated_web`;
- the authorized account or asset class it can read;
- supported source modes and their actual discovery horizon;
- required products, permissions, review, and ownership relationships;
- the music, transcript, subtitle, comment, reply, and metric fields it can
  legitimately attempt; and
- retention, deletion, rate-limit, and refresh obligations.

Live capabilities are adapter- and authorization-specific, not merely platform-wide.
For example, public YouTube lookup and an OAuth-authorized channel-owner adapter
do not have the same caption capability.

### Stage 1: Use the closed evidence and state substrate

`tiktok_scraper/social_music_contract.py` is the platform-neutral, closed evidence
boundary. It accepts already observed and explicitly attributed
metadata, normalizes only approved scalar fields, assigns terminal outcomes,
sanitizes unsafe transport values, and binds the capability manifest and evidence
to deterministic hashes. It does not fetch platform data or call a provider. Its
music fields are declarations rather than acoustic verification; acoustic and
lyrics states remain `not_attempted`.

`tiktok_scraper/social_music_state.py` is the durable `LISTEN` state substrate.
It freezes one platform, source mode, target, positive requested
count, capability-manifest hash, and adapter version; stores immutable,
hash-verified checkpoints; keys the global known-content registry by platform and
content ID; prevents conflicting or excess checkpoints; and finalizes only as an
exact complete count or an explicit incomplete result. It contains no browser or
platform client and no analysis, drafting, approval, engagement, or publication
stage.

`social_music_audit.py` exposes this substrate as an offline-only CLI. It accepts
caller-supplied collector JSON, applies the shared raw contract and safe evidence
projection, excludes globally known platform/content-ID pairs, checkpoints each
record, fails closed at the immutable requested count, and atomically exports only
a complete exact-count run. It has no browser, network, subprocess, AI, analysis,
approval, engagement, or publication integration.

The CLI's global `--database` option must precede the command:

```powershell
$EngagePython = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
& $EngagePython social_music_audit.py --database .\social_music.sqlite capabilities --platform youtube
& $EngagePython social_music_audit.py --database .\social_music.sqlite create --platform youtube --source-mode topic --target 'indie music' --posts 10 --run-id youtube_indie_10
& $EngagePython social_music_audit.py --database .\social_music.sqlite ingest --run-id youtube_indie_10 --file .\collector_records.json
& $EngagePython social_music_audit.py --database .\social_music.sqlite finalize --run-id youtube_indie_10 --reason source_exhausted
& $EngagePython social_music_audit.py --database .\social_music.sqlite export --run-id youtube_indie_10 --file .\youtube_indie_10.jsonl
```

These components are foundations, not a declaration that every capability in a
platform is officially available. Current `supported` values mean only that the
offline importer accepts explicitly attributed caller-supplied fields for that
platform. The CLI proves closed normalization, source-scope checks, durable hashes,
and exact imported cardinality; it does not prove that an official or experimental
adapter observed the supplied values. A production integration must bind and
verify the live adapter's authority tier, permissions, asset scope, and version in
an expanded manifest. It must also add a compliant deletion or
tombstone path where platform policy requires removal; immutable evidence alone is
not a complete retention implementation.

### Stage 2: Add official, read-only adapters independently

Each platform should be enabled separately and only for the source modes its
official API actually supports. A direct URL may be normalized to an immutable
platform content ID, a creator run may use an official owner/channel/page feed,
and topic discovery may be enabled only where an official search surface exists.
Unsupported source modes are rejected; they are never silently reinterpreted as
another mode or filled with browser scraping.

An official adapter must be able to return explicit terminal outcomes when review,
permissions, ownership, pagination, quota, deletion, age, moderation, or access
restrictions prevent collection.

### Stage 3: Isolate Instagram authenticated-web experiments

The repository's existing authenticated-web Instagram observations may expose
fields such as `music_info`, a platform music ID, title, artist, or original-sound
flag. Those fields are obtained from session-bound, undocumented web/GraphQL
responses. They are experimental and non-official. They must never be described
as Instagram Graph API support.

The current Instagram extractor labels these fields
`experimental_authenticated_web` and the offline projection preserves that
caller-attributed label. It does not independently authenticate the session or
endpoint. If promoted into a live research adapter, it must:

- be disabled by default and explicitly labeled
  `experimental_authenticated_web`;
- use a separate, hash-bound capability manifest and adapter version;
- retain field-level source and method provenance;
- fail closed when the response shape, session, or attribution changes;
- store no cookies, tokens, signed media URLs, or raw payloads; and
- report `unavailable`, `not_provided`, or `unsupported` rather than falling back
  to inference.

Experimental fields cannot make an official-only run complete. They cannot be
mixed into an official evidence block or used to claim a stable platform
contract. Official Instagram access currently provides only a coarse Reel audio
classification such as `media_audio_type` (`MUSIC` or `ORIGINAL_SOUND`); it does
not provide a normal track ID, title, artist, album, ISRC, duration, or catalog
link. That coarse value must remain distinct from track identity and must not be
mis-mapped into a title or artist field.

### Stage 4: Harden compliance and exact-count operation

Before production use, each adapter needs contract tests for permission loss,
pagination exhaustion, deleted content, rate limiting, duplicate IDs, conflicting
evidence, schema drift, and deletion requests. Exports must contain only the safe,
hash-verified evidence projection. Platform deletion events and user revocation
must be able to redact or tombstone affected retained data without rewriting the
historical fact that a run became incomplete or its evidence was removed for
compliance.

## Official capability matrix

The matrix describes documented official API capability, not browser-visible
features and not what a signed-in human can see on a website.

| Platform | Official access and discovery scope | Posts, comments, and metrics | Music attribution | Transcript and subtitle boundary | Consequence for a metadata-only audit |
| --- | --- | --- | --- | --- | --- |
| Instagram | The Instagram Platform is for professional accounts. Instagram Login uses permissions such as `instagram_business_basic`, `instagram_business_manage_comments`, and `instagram_business_manage_insights`; Facebook Login uses `instagram_basic`, `pages_show_list`, `pages_read_engagement`, `instagram_manage_comments`, and `instagram_manage_insights` with a linked Page. Serving accounts the app does not own/manage requires the applicable advanced access, App Review, and Business Verification. Own-media listing is capped at the 10,000 most recent items. Business Discovery targets an exact professional username. Reviewed hashtag discovery is limited to 30 unique hashtags per seven days, its recent set covers 24 hours, and it returns at most 50 items per page; neither surface is arbitrary-person or global public-post search. | Owned professional media can expose caption/description, type, product type, permalink, timestamp, counts, comments/replies, and authorized insights. The owned-media comments edge returns at most 50 top-level comments per query. Business Discovery and hashtag results expose narrower public projections; hashtag discovery does not grant comment bodies or music identity. | The official media resource can expose coarse `media_audio_type` for Reels. It does **not** expose a documented song/sound ID, title, artist, album, ISRC, track duration, or recording match suitable for the proposed music block. Copyright-related behavior is not a music declaration. | No documented Instagram Graph API provides a speech transcript, timed-text track, subtitle text, or caption-file download. A media `caption` is the post's written description, not spoken-word transcription. | Official collection can preserve post metadata, accessible comments/metrics, and a coarse audio-type outcome. Track identity and transcript/subtitle evidence normally terminate as `not_provided` or `unsupported`; an experimental web observation must remain separate. |
| Facebook | The Pages API works with Page access tokens and Page tasks. Typical read scopes are `pages_show_list`, `pages_read_engagement`, and, for visitor content, `pages_read_user_content`; insights use `read_insights`, while writes would separately require permissions such as `pages_manage_engagement` or `pages_manage_posts` and are outside this design. Page Public Content Access is a reviewed feature for public Page metadata, posts, and comments. It is not arbitrary profile, group, or global public-post discovery. | Page feeds/posts, video metadata, Page comments/replies/reactions, and authorized insights are available within endpoint-specific horizons and permissions. The Page feed is ranked and bounded to approximately 600 published posts per year with a maximum requested page size of 100. PagePost does not represent Reels, and official Reel read support must be verified against the deployed Graph version because current guide and endpoint-reference language differ. | No normal Page post, Video, or Reel read field provides per-post song/sound ID, title, artist, album, ISRC, or recording match. The Video API's music-recommendation surface recommends library music; it is not attribution for audio already used in an arbitrary post. Owner copyright checks are not a public music declaration. | The Video `captions` edge can expose caption-track objects, and authorized Page/video managers can upload SRT files. Official read documentation does not promise public caption text or SRT download for arbitrary videos, and it supplies no general speech transcript. | Page-owned or approved public-Page evidence can be collected within its horizon. Music identity is unsupported. Caption-track availability may be recorded when documented and authorized, but subtitle text and transcript text cannot be claimed without a documented, authorized retrieval path. |
| X | The X API supports lookup of one post or batches of up to 100 IDs and recent/full-archive search according to access tier and query rules. Recent search covers the previous seven days and returns at most 100 posts per page; full-archive search reaches back to 2006 and can return up to 500 per page where the product tier permits it. This is public-post search, but it remains bounded by product access, query operators, quotas, deletions, and visibility. | Post fields and expansions provide author/media/context metadata. Public metrics include replies, reposts, quotes, likes, and qualifying view/impression fields. Replies are separate posts normally discovered through conversation search; there is no universal nested-comments edge. Owner-only organic/non-public/promoted metrics require user context and are available only within their documented time window. | The documented post/media data dictionary provides media type, dimensions, duration, preview/variant metadata, accessibility alt text, and metrics, but no song/sound identity or declared track fields. | The documented API has no general speech-transcript field, subtitle-track inventory, timed-text field, or caption-file retrieval for a post's media. `alt_text` is accessibility text supplied for media, not a transcript. | Official topic/search and ID-scoped collection are possible within the purchased horizon. Music, transcript, and subtitle outcomes terminate as `unsupported` or `not_provided`; no media variant may be downloaded for inference. |
| YouTube | `videos.list` retrieves known public video IDs. `search.list` provides official public discovery and returns at most 50 results per page. Since June 1, 2026, default projects receive a separate allowance of 100 `search.list` calls per day, with one search request consumed per call, alongside the general quota used by other endpoints. Exact channel inventory should use the channel's uploads playlist and `playlistItems.list`. An API key can read public data, while OAuth-authorized owner operations have additional capabilities. | Video metadata includes title, description, channel, duration, statistics, and whether captions are available. `commentThreads.list` returns top-level threads; when embedded replies are incomplete, `comments.list` with the parent ID is needed. Metrics and comment availability depend on privacy, moderation, and authorization. | The YouTube Data API does not expose the watch page's “Music in this video” attribution as a documented per-video sound ID/title/artist/album/ISRC field. `licensedContent`, category, topics, and tags are not recording identity. | `video.contentDetails.caption` is only an availability indicator. `captions.list` returns caption-track metadata, not caption text. `captions.download` returns a track only with OAuth authorization and permission to edit the video, so it is unavailable for an arbitrary public video. The official API provides no general public speech-transcript endpoint. | Public runs can collect metadata, comments, metrics, and a terminal caption-availability outcome. Track text is eligible only for an adapter demonstrably authorized to list/download that video's caption tracks; otherwise transcript/subtitle text is unavailable. Undocumented watch-player or transcript endpoints are not official API evidence. |

## Exact-count and evidence contract

### Immutable run identity

A run freezes exactly one platform, one supported source mode, one normalized
target, one positive requested count `N`, one adapter version, and one capability
manifest hash. Cross-platform aggregation is a portfolio of independently frozen
platform runs; it is not one discovery loop in which a missing Instagram result
can be replaced by a YouTube result.

Every candidate is identified by the composite key `(platform, content_id)`. A
direct-URL run additionally freezes a sanitized canonical platform URL. A numeric ID from one platform is never compared or
deduplicated as though it belonged to another. Direct-URL collection binds the
exact ID and permits no substitution. The workspace-global known-content check is
also platform-scoped.

### What the implemented offline importer counts toward `N`

A caller-supplied record currently counts only when:

1. It has a platform/content-ID binding and, for creator or direct-URL scope,
   matches the frozen normalized creator or exact content ID. URL scope is fixed
   to one record.
2. Its allowlisted source, metric-availability, collector, comment-frontier,
   social-music, and identity envelope validates and contains no forbidden
   transport or credential fields.
3. Collector status is successful, comment collection is complete without source
   or local-storage truncation, and every manifest-supported music, transcript,
   and subtitle block records an attempted outcome with source, method, authority,
   access scope, and observation time.
4. Its evidence hash, adapter-bound manifest hash, uniqueness, ordinal, global
   platform/content-ID exclusion, and immutable requested count all validate.

This gate recalculates readiness from the stored envelope before status,
finalization, and export. Topic relevance and caller-attributed authority are not
independently verified. The current gate also does not require a nonempty creator,
media type, publication time, caption, metric value, or endpoint-authenticated
metric provenance. Those are production-adapter requirements below.

### Additional production-adapter gate

A live record must additionally satisfy all of the following before it may count:

1. Its platform, content ID, canonical URL, creator/channel/Page identity, media
   type, and observation time are bound.
2. Required public metadata and metrics have either safe values or explicit
   endpoint-specific availability outcomes.
3. Comments and replies available to the frozen adapter have been attempted to a
   recorded terminal frontier, or a terminal zero/unavailable outcome is stored.
4. Music-declaration, transcript, and subtitle capabilities named in the manifest
   have each reached a terminal outcome. Unsupported capability is a valid
   terminal observation; an unattempted supported capability is not.
5. Field-level authority, source, method, observed time, adapter version, and
   evidence hash pass closed-contract validation.
6. The record is durably checkpointed without conflicting with an ordinal,
   platform/content ID, existing evidence hash, or the immutable requested count.

The offline CLI never performs discovery; it imports supplied records and finalizes
as exact complete or explicit `collection_incomplete: X/N`. A future live adapter
must continue discovery and pagination until exactly `N` evidence-ready,
unique records are checkpointed. If the documented search horizon, profile/Page
inventory, permissions, moderation, quota, deletion, or access tier is exhausted,
the run finalizes as `collection_incomplete: X/N` with enumerated reasons. It never
silently accepts a smaller set, crosses to another platform, downloads media, or
uses an unofficial fallback.

### Terminal outcomes

Each imported capability uses the closed normalized vocabulary modeled by the
social music contract. These labels describe a caller-supplied import outcome; by
themselves they do not prove that an endpoint was called or that authorization was
valid:

- `available`: the minimum accepted value for that block was supplied (title and
  artist for a music declaration, transcript text, or at least one subtitle-track
  descriptor);
- `partial`: some attributed metadata was supplied but identity is incomplete or
  the reported surface was intentionally coarse;
- `not_provided`: the caller reports that an attempted surface supplied no
  declaration or value;
- `unavailable`: the caller reports an access/provider failure, or the importer
  lacks adequate attempt/provenance proof;
  and
- `unsupported`: the offline importer's platform table does not accept that block;
  official-product support remains a separate live-manifest decision.

Reasons must be endpoint- and adapter-specific. `not_provided` must not conceal a
permission failure, and `unsupported` must not be converted into `available` by
inference. A platform music declaration establishes only what the platform
declared; it never establishes acoustic identity. Transcript or subtitle metadata
never establishes lyrics.

## Safe evidence projection

The implemented safe envelope may retain:

- canonical platform/content identity and creator/channel/Page attribution;
- safe post description/caption text, media type, publication/observation times,
  and public metric values plus availability labels;
- up to 500 accessible comments/replies with platform IDs, counts, and a bounded
  completion/frontier summary (but not per-comment transport provenance);
- coarse platform-native music/audio declarations and, only when actually
  returned, explicitly attributed track identity fields;
- caller-attributed transcript text and up to 20 explicit subtitle-track
  descriptors; subtitle text is not retained and transcript fields are never used
  to fabricate subtitle inventory;
- explicit availability and terminal reasons for every configured block; and
- capability-manifest, normalized-evidence, and result hashes.

The safe projection excludes media bytes, preview/playback/caption-download URLs,
avatars when not needed for identity, provider raw responses, cookies, tokens, and
authorization material. It must preserve `acoustic_verification.status =
not_attempted`, `acoustic_verification.verified = false`, and
`lyrics.status = not_attempted` for every platform in this design.

## Retention, deletion, and refresh

Platform access is not permission to retain data forever.

- Meta requires prompt deletion when data is no longer needed for a legitimate
  purpose, the service stops, Meta requests deletion, the user requests deletion
  or removes the app, or law requires it. A Meta adapter therefore needs asset and
  user revocation handling in addition to ordinary refresh.
- X supplies compliance mechanisms for post and account state changes. Retained X
  records must be reconciled with applicable deletion, withholding, and suspension
  events rather than treated as permanently public.
- YouTube policies restrict downloading or storing audiovisual content and impose
  deletion/refresh duties for authorized data and revoked access. This design's
  no-media rule is mandatory even when a media URL is visible in an API response.

Evidence hashes can prove what was stored, but they do not override a deletion
obligation. A compliant implementation should replace removed platform data with a
minimal tombstone that preserves platform/content identity, deletion reason,
deletion time, and affected run references where policy permits. It must not keep
the removed payload merely to preserve an earlier hash.

## What a cross-platform MUSIC AUDIT can and cannot claim

It can claim that a named official or explicitly experimental adapter observed
specific, provenance-bound metadata at a stated time; that accessible comments,
metrics, and documented caption availability were attempted; and that every
configured evidence block ended with an explicit terminal outcome.

It cannot claim that every public post was discoverable, that browser-visible data
is an official API field, that a coarse music/audio category identifies a song,
that caption availability provides transcript text, that alt text is spoken-word
transcription, that a platform declaration acoustically matches the media, or that
missing lyrics or speech were inferred. It also cannot promise exact `N` where the
official source horizon is exhausted; the correct durable result is the verified
`X/N` incomplete outcome.

## Official references

### Instagram and Facebook

- [Instagram Platform overview](https://developers.facebook.com/documentation/instagram-platform/overview)
- [Instagram Media reference](https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media)
- [Instagram User media edge](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media)
- [Instagram Business Discovery](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/business-discovery)
- [Instagram Hashtag Search](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/hashtag-search)
- [Instagram Media comments edge](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-media/comments)
- [Instagram Comment reference](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-comment)
- [Instagram Insights overview](https://developers.facebook.com/documentation/instagram-platform/insights)
- [Instagram Media insights](https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media/insights)
- [Pages API overview](https://developers.facebook.com/documentation/pages-api/overview)
- [Pages Search](https://developers.facebook.com/documentation/pages-api/search-pages)
- [Page Public Content Access](https://developers.facebook.com/docs/apps/features-reference#page-public-content-access)
- [Page feed reference](https://developers.facebook.com/docs/graph-api/reference/v25.0/page/feed)
- [PagePost reference](https://developers.facebook.com/docs/graph-api/reference/v25.0/page-post)
- [Facebook Reels publishing guide](https://developers.facebook.com/documentation/video-api/guides/reels-publishing)
- [Page video_reels reference](https://developers.facebook.com/docs/graph-api/reference/v25.0/page/video_reels)
- [Video API music recommendations](https://developers.facebook.com/documentation/video-api/guides/music-recommendations)
- [Video captions edge](https://developers.facebook.com/docs/graph-api/reference/v25.0/video/captions)
- [Video insights edge](https://developers.facebook.com/docs/graph-api/reference/v25.0/video/video_insights)
- [Pages comments and mentions](https://developers.facebook.com/documentation/pages-api/comments-mentions)
- [Meta Graph API rate limiting](https://developers.facebook.com/docs/graph-api/overview/rate-limiting)
- [Meta Platform Terms](https://developers.facebook.com/terms/)

### X

- [X post lookup](https://docs.x.com/x-api/posts/lookup/introduction)
- [X post search](https://docs.x.com/x-api/posts/search/introduction)
- [X metrics](https://docs.x.com/x-api/fundamentals/metrics)
- [X API data dictionary](https://docs.x.com/x-api/fundamentals/data-dictionary/reference)
- [About the X API](https://docs.x.com/x-api/getting-started/about-x-api)
- [X batch compliance](https://docs.x.com/x-api/compliance/batch-compliance/introduction)

### YouTube

- [YouTube Videos resource](https://developers.google.com/youtube/v3/docs/videos)
- [YouTube videos.list](https://developers.google.com/youtube/v3/docs/videos/list)
- [YouTube search.list](https://developers.google.com/youtube/v3/docs/search/list)
- [YouTube playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- [YouTube commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list)
- [YouTube comments.list](https://developers.google.com/youtube/v3/docs/comments/list)
- [YouTube captions.list](https://developers.google.com/youtube/v3/docs/captions/list)
- [YouTube captions.download](https://developers.google.com/youtube/v3/docs/captions/download)
- [YouTube captions implementation guide](https://developers.google.com/youtube/v3/guides/implementation/captions)
- [YouTube quota and compliance audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- [YouTube quota cost calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
- [YouTube API revision history](https://developers.google.com/youtube/v3/revision_history)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
