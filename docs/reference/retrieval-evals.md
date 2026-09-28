# Retrieval evals

A reusable, CI-runnable way to measure retrieval quality against a real
deployment: give `inherent eval run` a dataset of queries and the ids each
one SHOULD retrieve, and it reports hit@k and MRR.

This is distinct from the traffic-mined eval runs described in
[ADR 0003](../adr/0003-traffic-mined-retrieval-evals.md) (`POST
/v1/evals/*`), which mine an operator's own search traffic into labeled
cases. This harness is for a hand-authored, version-controlled ground-truth
set — the shape you'd pin in CI to catch a retrieval regression before it
ships, or that any vertical pack can carry alongside its own fixtures.

## Usage

```bash
inherent eval run --dataset evals/support.yaml
inherent eval run --dataset evals/support.yaml --k 5 --min-hit-rate 0.7
inherent --json eval run --dataset evals/support.yaml --workspace ws_abc
```

It calls `POST /v1/search` on the resolved stack (the same connection
`inherent search` uses) — no database access, no in-process test harness —
so it works against any deployment: a laptop, a Compose stack, or a staging
deployment in CI.

- `--dataset` (required): path to the dataset file (see format below).
- `--k`: top-k cutoff. Overrides the dataset's own `k`; defaults to 5 if
  neither is given.
- `--min-hit-rate`: exit code `1` (instead of `0`) when the mean hit@k falls
  below this threshold — the knob a CI job gates on.
- `--workspace`: workspace to search. Overrides the global `--workspace`.
- `--json`: machine-readable report instead of the human-readable summary.

## Dataset file format (YAML)

```yaml
name: support-eval-v1   # optional, defaults to the file's stem
k: 5                     # optional, overridden by --k
queries:
  - id: q1                # optional, defaults to the query's 1-based position
    query: "how do I reset my password"
    expected_ids: ["doc_123", "chunk_456"]
  - id: q2
    query: "refund policy"
    expected_ids: ["doc_789"]
    filters:               # optional -- a vertical pack's tag filters (#390)
      category: "billing"
```

`expected_ids` matches a returned result on EITHER its `document_id` or its
`chunk_id` — whichever the id names. This covers "the right document
matched" and "the right chunk matched" with one field, without needing a
compound `document_id#section`-style key: `SearchResult` already carries
both ids, so there is nothing extra to compute.

`filters` (optional) passes straight through to `SearchRequest.filters`
(#390) — a workspace bound to a vertical pack can eval filtered queries
exactly as an agent would issue them.

## Metrics

- **hit@k**: for each query, `1.0` if ANY expected id appears in the top `k`
  results, else `0.0`. The report's `hit_rate` is the mean across queries —
  "what fraction of queries got a usable result at all."
- **MRR** (mean reciprocal rank): for each query, `1 / rank` of the first
  expected id in the results (`0.0` if none appear); the report's `mrr` is
  the mean across queries — rewards ranking a hit higher, not just getting
  one.

Both are computed by `inh_contracts.ranking_metrics` (`hit_at_k` / `mrr`) —
the same implementation `inh-public-api-svc`'s own eval runner uses, so a
number from this harness and a number from the traffic-mined eval runs are
directly comparable.

## Wiring into CI

```bash
inherent eval run --dataset evals/support.yaml --min-hit-rate 0.7 || exit 1
```

A non-zero exit fails the job. `--json` gives a structured report
(`{dataset, k, query_count, hit_rate, mrr, queries: [...]}`) for a CI system
that wants to post a summary rather than parse stdout.

## Per-workspace hybrid alpha

Hybrid search fuses BM25 and vector search with a fusion weight `alpha`
(`1.0` = vector-heavy, `0.0` = keyword-heavy). A request's own `alpha` always
wins; when it's omitted, a workspace can be given a different DEFAULT via
the `WORKSPACE_HYBRID_ALPHA` operator setting on `inh-public-api-svc`:

```bash
WORKSPACE_HYBRID_ALPHA=ws_a=0.3,ws_b=0.5
```

A keyword-heavy domain (short, jargon-dense queries; exact-term matching
matters more than semantic similarity) can lower its workspace's default
alpha this way instead of every caller having to pass `alpha` on every
request. Unset (the default) leaves every workspace's alpha at the global
default (0.7, unchanged from before this setting existed). Malformed entries
fail the service to start, matching `WORKSPACE_VERTICAL_PACKS`'s "fail
loudly, not later" contract — see
[Vertical packs' workspace binding](vertical-packs.md#workspace-binding)
for the identical parsing shape.

Use `inherent eval run` to measure the effect of a candidate alpha on your
own corpus before configuring it.
