# ADR 0004 — Per-Document Result Diversification

- **Status:** Accepted. **Amended 2026-08-06** — maintainer approval granted,
  default flipped from `False` to `True`. See
  [Amendment](#amendment-2026-08-06-maintainer-approval-granted-default-flipped-to-true)
  for why now and what changed.
- **Date:** 2026-07-23
- **Deciders:** maintainers
- **Related:** [ADR 0003](0003-traffic-mined-retrieval-evals.md), #146, #47

## Context

A search request's ranked results come from over-fetching and truncating (or
just truncating) a score-sorted candidate list per `docs/advanced-indexes.md`
and `src/services/search.py`. A document that chunks into many pieces — a long
reference doc, a deep-dive, a spec — can occupy every slot in a small `limit`
(the common case: the default page size is small) purely because its chunks
all score well against the query, even when a shorter, differently-worded
document is also genuinely relevant and answers the query just as well or
better for some callers.

This is not hypothetical. Added to the golden corpus (`tests/evals/corpus/`)
as `q14` / category `multi_doc_crowding`: `rate-limiting-deep-dive.txt` (5
chunks, all on-topic for "how does Inherent enforce per-API-key rate limits")
and `rate-limit-quick-reference.txt` (1 chunk, differently worded, also
on-topic and judged equally relevant). Measured locally against a live Compose
stack with the production-default settings (`enable_diversification=False`):
the naive top-5 for every mode (semantic/hybrid/keyword) returns **only**
`rate-limiting-deep-dive.txt` — the quick-reference document is not fetched at
all, let alone ranked, because Weaviate returns exactly `limit` rows and every
one of them belongs to the longer document. `recall@5` for that query sits at
`0.5` (1 of 2 relevant documents retrievable) regardless of mode.

## Decision

> **Amended 2026-08-06.** The "opt-in, off-by-default" framing below is
> 2026-07-23's original decision. The flag now defaults to `True` — see
> [Amendment](#amendment-2026-08-06-maintainer-approval-granted-default-flipped-to-true).
> Kept below as the historical record of what was originally accepted and
> because points 1-2 (the mechanism itself) are unchanged by the amendment.

Add per-document diversification (`SearchService._diversify_by_document`,
`src/services/search.py`) as an **opt-in, off-by-default** post-filter:

1. When `enable_diversification` is on, widen the Weaviate fetch to
   `min(100, limit * diversification_over_fetch_multiplier)` (default
   multiplier `5`) instead of fetching exactly `limit` rows — there must be
   more candidates than the page size for diversification to have anything to
   diversify across.
2. Round-robin one result per `document_id`, in document order (each
   document's own best score, since candidates arrive score-sorted from
   Weaviate) and in within-document score order, until `limit` is reached or
   every candidate is exhausted.
3. When the flag is off (originally, and still, the byte-for-byte fallback
   path — just no longer the default since the 2026-08-06 amendment),
   behavior is identical to before this ADR: `results[:limit]`, no wider
   fetch, no round-robin.

### Why gated, not on by default (2026-07-23 original; superseded 2026-08-06)

> **Amended 2026-08-06.** This subsection explains why the flag shipped
> off by default on 2026-07-23. The gate it describes has since cleared — see
> [Amendment](#amendment-2026-08-06-maintainer-approval-granted-default-flipped-to-true).
> Kept below as the historical record of the gating rationale, which still
> applies to the mechanism (an operator can still turn diversification off).

This is not scaffolding like the #47 advanced methods (cross-encoder rerank,
GraphRAG, hierarchical index) — it is fully implemented, deterministic, and
requires no new model or index. It shipped gated behind the same eval-gate
policy (`enable_diversification`, default `False` at the time; requires a
documented eval improvement + maintainer approval before defaulting on)
because:

- It **changes ranking order** for every multi-chunk-per-document query, not
  just crowded ones — a caller depending on today's exact ranking for a
  well-served query could see its position shift even though nothing about
  that query's own relevance changed.
- The compose retrieval-eval gate (`test_compose_retrieval_regression.py`,
  ADR 0003's CI suite) needs to measure it against the full golden corpus over
  time before it earns production-default status, same bar as any #47 method.
- The over-fetch itself has a real cost (up to 5x the Weaviate query size)
  that should be paid only where the caller has opted in, not on every
  request by default.

### Measured evidence (local Compose run, 2026-07-23)

Flag off vs. flag on, `multi_doc_crowding` category specifically:

| Mode | recall@5 (off → on) | nDCG@5 (off → on) |
|---|---|---|
| hybrid | 0.5 → 1.0 | 0.613 → 0.920 |
| keyword | 0.5 → 1.0 | 0.613 → 0.877 |
| semantic | 0.5 → 1.0 | 0.613 → 0.920 |

Pooled per-mode metrics across the **whole** corpus (not just the new query),
flag off vs. on — every metric flat or improved, none regressed:

| Mode | recall@5 | nDCG@5 | MRR |
|---|---|---|---|
| hybrid | 0.846 → 0.885 | 0.720 → 0.744 | 0.795 → 0.795 |
| keyword | 0.808 → 0.885 | 0.714 → 0.744 | 0.821 → 0.821 |
| semantic | 0.846 → 0.962 | 0.681 → 0.734 | 0.695 → 0.710 |

This is exactly the shape of evidence the eval-gate policy asks for — a
documented improvement with no regression — but on 2026-07-23 it was one
measurement on one small golden corpus, not the sustained CI history the
policy originally expected before a flag defaults on. **As of the 2026-08-06
amendment, this evidence plus maintainer approval were judged sufficient to
flip the default** — see the Amendment section.

> **Note (2026-08-06):** the committed `corpus/retrieval_baseline.json` still
> reflects the flag-**off** numbers as of this writing — it is deliberately
> *not* re-seeded by the amendment (see the Amendment section for why), so it
> temporarily no longer matches the production default (`True`). Do not treat
> `retrieval_baseline.json`'s comment claiming "production default" as
> current until a follow-up ratchets it; the source of truth for the current
> default is `settings.py`.

## Boundary: what this is not

- **Not a ranking model change.** No score is recomputed; diversification only
  reorders which already-scored candidates survive truncation.
- **Not a fix for single-document corpora.** With one document in a workspace,
  or a query where only one document is ever relevant, diversification cannot
  help and does not change behavior (a single bucket round-robins with itself,
  equivalent to a plain truncate).
- **Not on by default as originally shipped (2026-07-23).** Flipping the
  default required the same eval-gate + maintainer approval process as any
  #47 method — both conditions were met and the default flipped to `True` on
  2026-08-06; see the Amendment section. This bullet's *mechanism* claim
  (single-document workspaces are unaffected either way) is unchanged.

## Consequences

- Callers with document collections containing long, multi-chunk documents
  alongside shorter authoritative ones (FAQs, quick-reference sheets, policy
  summaries) gain an opt-in way to avoid one document silently monopolizing
  the result page.
- `docs/advanced-indexes.md` and `settings.py` document
  `diversification_over_fetch_multiplier` as the tunable controlling the
  fetch/diversity tradeoff; a higher multiplier surfaces more distinct
  documents at the cost of a larger per-request Weaviate fetch.
- The golden corpus (`tests/evals/corpus/qrels.jsonl`) now carries a
  permanent `multi_doc_crowding` category (`q14`) so future changes to
  chunking, scoring, or diversification itself are measured against this
  scenario going forward, not just the categories ADR-0003's original corpus
  covered.
- Turning this on by default in a future release is a ranking-order change
  for existing callers and should ship as an explicit, changelogged decision
  (with fresh CI-measured evidence, not just this single local run) — not a
  silent default flip. **Done 2026-08-06** — see
  [Amendment](#amendment-2026-08-06-maintainer-approval-granted-default-flipped-to-true)
  and the `[Unreleased]` CHANGELOG entry; not a silent flip.

## Amendment (2026-08-06): maintainer approval granted, default flipped to `True`

**This ADR was amended on 2026-08-06.** `enable_diversification`'s default
changed from `False` to `True` in `src/config/settings.py`. This is a
deliberate decision change, not a correction of a factual error in the
2026-07-23 text above (contrast [ADR 0002](0002-weaviate-multi-tenancy-scale.md),
where the amendment fixes an ADR that was wrong about what already shipped).

### Why now

Both conditions this ADR's original "why gated" section required were met:

1. **Documented eval improvement.** Unchanged since 2026-07-23 — recall@5
   0.5 → 1.0 on the `multi_doc_crowding` golden-corpus category, every pooled
   per-mode metric flat or improved, none regressed (see "Measured evidence"
   above).
2. **Maintainer approval.** Granted 2026-08-06.

The immediate trigger was a sibling change on the same integration branch:
**#129 (format-aware chunking)** changed `sample.json`'s extraction from 1
chunk to 4. With diversification still off, those four same-document chunks
crowded the keyword-mode top-5 on the golden corpus — a measured regression
(`keyword.mrr` 0.8205 → 0.7821, `keyword.ndcg@5` 0.7137 → 0.6835, both beyond
the 0.02 gate tolerance) with `recall@5` unchanged in all three modes: the
right document stayed retrievable, only its rank slipped. That is exactly
the self-crowding failure mode this ADR exists to prevent, reproduced by an
unrelated change rather than by the `multi_doc_crowding` fixture — evidence
that the risk this ADR flagged ("a document that chunks into many pieces...
can occupy every slot") is not confined to the one golden-corpus query it was
originally measured against.

### What this amendment does and does not change

- Changes: `enable_diversification`'s default (`settings.py`), the tests
  that pinned the old default (`tests/unit/test_search_diversification.py`,
  `tests/evals/test_advanced_index_gate.py`), and the docs/CHANGELOG language
  describing this as opt-in.
- Does not change: the mechanism (`_diversify_by_document`, the over-fetch
  widening in `_build_graphql`) — both are byte-for-byte the same code as
  2026-07-23; only the flag's default value moved.
- Does not re-seed `corpus/retrieval_baseline.json`. The committed baseline
  keeps reflecting flag-**off** numbers deliberately, so the compose
  retrieval-eval gate (`test_compose_retrieval_regression.py`) measures the
  new, flag-**on** default against the old, flag-off baseline on its own
  merits in CI, on the same PR that flips the default and carries #129 —
  this satisfies the original ADR's "fresh CI-measured evidence, not just
  this single local run" bar before the change merges. If that CI run shows
  the gate still fails, re-baselining is a separate, deliberate follow-up,
  not folded into this amendment.

### Operator impact

An operator who wants the pre-2026-08-06 ranking behavior sets
`ENABLE_DIVERSIFICATION=false` (env var, `.env`, or the Compose override in
`docker-compose.yml`). No other behavior changes; `_diversify_by_document`
and the over-fetch widening are unchanged code, only reachable by more
callers by default now.
