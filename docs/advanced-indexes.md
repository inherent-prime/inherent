# Advanced retrieval indexes (eval-gated) — #47

> **Status: SCAFFOLDING ONLY.** All three methods below are **EXPERIMENTAL,
> OFF BY DEFAULT, and NOT IMPLEMENTED.** The flags exist and the dispatch point
> is wired, but no graph / rerank / hierarchy logic has been added. Enabling a
> flag today only emits a "enabled but not implemented (scaffolding)" log line
> and changes nothing about the results.

The `inh-public-api-svc` search service ships three **standard** retrieval modes
(`semantic`, `hybrid`, `keyword` — see `SearchRequest.search_mode`). This
document describes three **advanced** retrieval methods that are planned on top
of those modes, why they are off by default, and the policy that governs when —
if ever — one of them may be turned on by default.

## The three advanced methods

| Method | Settings flag | What it will do (planned) |
| --- | --- | --- |
| **Cross-encoder rerank** | `enable_reranker` | Re-score the assembled top-k results with a cross-encoder for sharper ordering. |
| **GraphRAG index** | `enable_graphrag_index` | Retrieve over a GraphRAG-style entity/relationship graph index, not just chunk vectors/BM25. |
| **Hierarchy index** | `enable_hierarchy_index` | Retrieve over a hierarchical (parent/child / summary) index for better long-document recall. |

All three flags live in `src/config/settings.py` and **default to `False`**.

## Why off by default

The production default is the **measured hybrid baseline** established in #45 and
exercised by the **M4 retrieval evals** (`services/inh-public-api-svc/tests/evals/`,
metrics: recall@k, MRR, nDCG@k). Advanced methods add cost and complexity and can
*regress* quality if added blindly, so none of them ships on until it has *proven*
it helps on that baseline.

As of #139, that baseline is a **hard, ratcheting CI gate**
(`corpus/retrieval_baseline.json`, checked by `tests/evals/eval_gate.py`), not
just a documented number — see
[docs/testing.md § Retrieval-eval gate](testing.md#retrieval-eval-gate-baseline-ratchet-and-trend-history-139).

## Eval-gate policy

**No advanced method may be enabled by default without BOTH:**

1. **A documented eval improvement vs the hybrid baseline (#45).** The method
   must show a measured improvement on the M4 retrieval evals
   (`tests/evals/`) relative to the current hybrid baseline — improvement
   documented (numbers + which corpus/queries), not asserted.
2. **Maintainer approval.** A maintainer must sign off on the result and the
   default-on change (see `docs/maintainers/`).

Until both are met, the flag stays `False` by default and may only be turned on
explicitly in dev for experimentation. The defaults are themselves asserted by
`tests/evals/test_advanced_index_gate.py` so the gate cannot be silently
defeated.

### Per-method eval target (PLACEHOLDER thresholds)

These are **placeholder** acceptance thresholds to be finalized with real
measurements; each is "must not regress, and must clear the bar below" vs the
hybrid baseline on the M4 corpus. Stated at `@5` because that is the cutoff
the compose retrieval-eval gate actually computes today
(`test_compose_retrieval_regression.py`'s `recall_at_k`/`ndcg_at_k` calls pass
`k=5`, matching `corpus/retrieval_baseline.json`'s `recall@5`/`ndcg@5` keys) —
a method cleared against `@10` numbers would not be measurable against the
gate that exists:

| Method | Placeholder target (vs hybrid baseline #45) |
| --- | --- |
| Cross-encoder rerank | nDCG@5 improvement >= +0.03 (no recall@5 regression) |
| GraphRAG index | recall@5 improvement >= +0.05 (no nDCG@5 regression) |
| Hierarchy index | recall@5 improvement >= +0.05 on long-document queries (no nDCG@5 regression) |

## How to enable in dev (experimentation only)

Set the corresponding environment variable before starting the service, e.g.:

```bash
export ENABLE_RERANKER=true
export ENABLE_GRAPHRAG_INDEX=true
export ENABLE_HIERARCHY_INDEX=true
```

(or the equivalent keys in your `.env`). With a flag on, the service logs
`advanced method '<name>' enabled but not implemented (scaffolding)` and returns
results **unchanged** — this is expected until the method is implemented and
clears the eval gate above.

## Where it is wired

- Flags: `services/inh-public-api-svc/src/config/settings.py`
- No-op dispatch: `SearchService._apply_advanced_methods(results, request)` in
  `services/inh-public-api-svc/src/services/search.py`, called after results are
  assembled in `SearchService.search()`.
- Gate test: `services/inh-public-api-svc/tests/evals/test_advanced_index_gate.py`

## Per-document diversification (#146) — implemented, on by default

Unlike the three scaffolding-only methods above, per-document diversification
(`enable_diversification`, `SearchService._diversify_by_document`) **is fully
implemented** — no new model or index, just a wider Weaviate fetch and a
round-robin over already-scored candidates before truncating to the page
size. It was gated behind the same eval-gate policy as the #47 methods
(documented eval improvement + maintainer approval before defaulting on) for
a different reason: it changes ranking order for every
multi-chunk-per-document query, not just crowded ones, so a caller relying on
today's exact ranking for an already-well-served query could see it shift
even though that query's own relevance hasn't changed.

**Both gate conditions are now met and the default is `True` as of
2026-08-06.** See [ADR 0004](adr/0004-per-document-diversification.md) and
its 2026-08-06 amendment for the measured evidence (recall@5 0.5 → 1.0 on the
golden corpus's `multi_doc_crowding` category, no regression on any other
category/mode) and the maintainer approval that cleared the gate. Its own
tunable, `diversification_over_fetch_multiplier` (default `5`), controls how
many extra candidates are fetched per request when the flag is on; ignored
when it's off. An operator who wants the pre-2026-08-06 ranking behavior sets
`export ENABLE_DIVERSIFICATION=false` (or the Compose override documented
inline in `docker-compose.yml`).
