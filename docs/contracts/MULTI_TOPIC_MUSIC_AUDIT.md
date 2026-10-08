# Multi-Topic MUSIC AUDIT Contract

`AGENTS.md` is controlling when this document differs. This contract defines
only the durable `music_audit_topics.py` coordinator for an explicit MUSIC
AUDIT request containing multiple topics. It does not add a canonical source
mode: every executable child remains one ordinary TikTok topic MUSIC AUDIT
stored as `workflow=listen`.

The coordinator is collection-only. It cannot invoke semantic AI, score or
analyze content, download audio, draft or review a response, create approval or
publication state, contact another platform, engage, or publish.

## Exact query rule

Every new child freezes `topic_query_policy=exact`. After ordinary whitespace
normalization, the user's topic is the only TikTok search query for that child.
The child may paginate or retry that same query and build a larger candidate
reserve, but its quota, page budget, duplicate filtering, evidence failures,
and retry logic never generate prefixes, suffixes, location or language terms,
commercial-intent phrases, synonyms, related keywords, or other variations.

A real exact-query frontier, access refusal, or bounded terminal failure before
the fixed quota is met produces `collection_incomplete: X/N`. It is not
permission to broaden the query, lower the quota, borrow from another topic, or
claim completion. Existing single-topic runs whose immutable saved policy is
`related_variants_v1` remain valid legacy state and resume only through their
exact saved run/handoff. The coordinator never creates such a child.

## Required planning scope

Planning requires at least two ordered, nonempty topics and exactly one quota
meaning. Normalize surrounding/repeated whitespace for identity and reject
case-insensitive normalized duplicates. Do not alphabetize or otherwise change
the user's input order.

### TOTAL

```powershell
& $AuditPython .\music_audit_topics.py plan `
  --count-mode total --posts 1000 `
  --topic 'mr diy' --topic 'ace hardware'
```

`--posts N` is one positive total. Reject `N < topic_count`, because every
child quota must be positive. Assign `floor(N/topic_count)` to each topic, then
give one additional post to each of the first `N mod topic_count` topics in
input order. This allocation is frozen; TOTAL is not a fungible pool.

### EACH

```powershell
& $AuditPython .\music_audit_topics.py plan `
  --count-mode each --posts 500 `
  --topic 'mr diy' --topic 'ace hardware'
```

Assign the same positive `N` to every topic. The parent requested total is
`N * topic_count`.

### CUSTOM

```powershell
& $AuditPython .\music_audit_topics.py plan `
  --topic-quota 'mr diy=700' `
  --topic-quota 'ace hardware=300'
```

Each repeated `--topic-quota 'TOPIC=N'` supplies one ordered topic and its
positive integer quota. Reject a missing, zero, negative, malformed, or
duplicate topic/quota. CUSTOM cannot be combined with `--topic`, `--posts`, or
`--count-mode`.

If the user supplies multiple topics but it is unclear whether a number means
TOTAL, EACH, or topic-specific CUSTOM quotas, ask for that choice. Never
concatenate topics into one search query, infer weights, invent related
searches, or silently choose an allocation.

## Immutable parent and child bindings

`plan` is offline. It creates a unique parent beneath:

```text
comments_data/music_audit_topic_runs/<run-id>/
```

The CLI has no alternate output-root option. A copied, moved, symlinked, or
otherwise relocated parent is rejected, even when its manifest bytes are
unchanged.

The parent manifest freezes at least the schema/version, parent identity,
ordered normalized topics, original ordinal, count mode, per-topic quota,
parent requested total, allocation rule, global `new_only` policy, exact-query
policy, canonical guarded-operator identity, intended child project identity,
and an integrity hash over the complete plan. A child binding becomes durable
when started and must include its exact project directory, database, handoff,
run ID, requested quota, and terminal/validation outcome. Parent records and
reviews are summaries and links; full evidence remains in each child's normal
canonical `comments_data/project_music_audit_*` directory and master lineage.

Continuation, status, and validation must reject a changed topic, order,
quota, parent total, allocation rule, plan hash, child path, handoff, run ID, or
canonical child evidence binding. Never edit or reconstruct a manifest to make
a run pass.

## Sequential collection and overlap

Start live work only with the exact planned directory:

```powershell
& $AuditPython .\music_audit_topics.py collect `
  --run-dir '<exact-run-directory>'
```

The coordinator starts at most one child at a time, in frozen topic order.
Each child is an ordinary canonical guarded MUSIC AUDIT topic run with:

- the exact normalized topic and fixed child quota;
- `workflow=listen`, `collection-policy=new_only`, and
  `topic_query_policy=exact`;
- the required Profile 7 startup/account preflight owned by the canonical
  collector;
- full metadata, transcript/subtitle, accessible comment/reply, TikTok music,
  configured-catalog, checkpoint, evidence-hash, exact-count, and master-
  registry gates; and
- no AI or outbound stage.

A later child starts only after the earlier child is complete and its canonical
export/review validates. Stop the parent at the first running, interrupted,
blocked, incomplete, or invalid child.

All children use the workspace-global master registry. If one TikTok post
matches several topics, the earliest child that checkpoints it owns that post;
later children exclude its ID under `new_only` and must discover other
globally-new IDs. This deterministic first-topic ownership is the only overlap
rule. Do not double-count an ID, move it between children, or rewrite topic
attribution after collection.

Every child quota remains fixed. Never borrow, pool, rebalance, or redistribute
a shortfall to another child, even under TOTAL mode. A parent that has 1,000
records in aggregate but an underfilled child is incomplete.

## Continuation and offline commands

Use only the saved parent directory:

```powershell
& $AuditPython .\music_audit_topics.py continue --run-dir '<exact-run-directory>'
& $AuditPython .\music_audit_topics.py status --run-dir '<exact-run-directory>'
& $AuditPython .\music_audit_topics.py validate --run-dir '<exact-run-directory>'
```

`continue` is a live operation only after the non-AI user explicitly requests
continuation. It must inspect the current child's guarded state and obey it
literally: wait on `RUNNING`, finalize offline when authorized, execute only the
returned exact same-handoff continuation for `INTERRUPTED` or a user-directed
`RESUME_READY`, and preserve/report `BLOCKED`. It must not run a low-level
browser preflight first, create a replacement child, skip ahead, or spend an
unauthorized resume attempt. Once the exact child validates complete, the
coordinator may proceed sequentially to the next frozen child.

`status` and `validate` are offline. They must not start or attach to Profile 7,
contact TikTok or a catalog provider, mutate a child/master database, or change
the parent plan. Saved parent status is not proof of process liveness; guarded
child `poll` remains authoritative while a child may be active.

## Completion

The parent is complete only when every child is canonically
`collection_complete`, contains exactly its requested number of unique
evidence-ready post IDs, has a valid project/master/evidence/export binding,
and passes parent validation against the frozen plan. Report the count mode,
ordered topics and quotas, each child identity and `X/N` result, aggregate
requested and validated totals, overlap policy, parent/child artifact paths,
plan/validation hashes, blockers, and empty AI/outbound action lists.

Successful completion writes a hash-bound `review.json` under the canonical
parent. An explicit offline `validate` rechecks every child, including earlier
children after later topics finish, proves cross-child post-ID uniqueness, and
writes `validation.json` in that same parent. Child database, export, machine
review, post-set, and validation hashes remain exposed in the parent result;
the full evidence stays in each canonical child project.

Any underfilled, blocked, running, interrupted, missing, mismatched, or invalid
child keeps the parent incomplete. Preserve all durable evidence and report the
bounded reason; never hide a shortfall with a new project or a changed plan.
