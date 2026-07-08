# ADR 0003 — Traffic-Mined Retrieval Evals

- **Status:** Accepted (initial draft)
- **Date:** 2026-07-08
- **Deciders:** maintainers
- **Related:** [ADR 0001](0001-agent-memory-substrate.md), [ADR 0002](0002-weaviate-multi-tenancy-scale.md)

## Context

Inherent can index a corpus and serve retrieval, but it cannot **prove
retrieval quality on the operator's own data**. A trial evaluator uploads their
documents, runs a few searches, sees plausible-looking results — and has no way
to judge whether retrieval is actually good without hand-building an eval
harness (a golden question set, relevance labels, a scoring script). Adoption
then defaults to vibes, and vibes default to the incumbent tool.

The same gap bites after adoption: an operator who re-chunks, changes embedding
config, or upgrades has no way to tell whether retrieval **regressed on their
corpus**. Retrieval regressions surface downstream as answer-quality
regressions, get blamed on the model or the prompt, and turn into cross-boundary
archaeology.

The maintainers already run a CI eval suite (`tests/evals/`) against a fixed
golden corpus. That proves the *product* works on *our* data; it does nothing
for an operator asking "does it work on *mine*?" This ADR is about closing that
operator-facing gap.

**Design metric:** *time-to-first-verdict* — an operator must get a defensible
retrieval-quality number for their corpus, from their own queries, within one
afternoon, with zero eval authoring.

## Decision

Add **traffic-mined retrieval evals** to `inh-public-api-svc`: mine real search
traffic and agent feedback into a labeled eval set, then replay it to score
retrieval on the operator's live corpus. The load-bearing decisions, each with
the alternative it was chosen over:

### Ownership follows the knobs

A system should own the evals for the variables it controls.

| Who | Eval tool | Owns |
|---|---|---|
| Inherent maintainers | `tests/evals/` CI suite (existing) | Product quality; code regressions |
| Operator (deployment owner) | **This feature** | Retrieval quality on their corpus; chunking/config tuning |
| Consumer agents | `report_feedback` + `get_retrieval_health` (MCP) | Supplying ground truth; calibrating trust |

Inherent owns **retrieval-layer evals** (recall / MRR / nDCG, verdict rates,
corpus gaps) because only the system that indexed the corpus can replay queries
against the index and knows what relevant material exists that *wasn't*
returned. The consumer owns **answer/task-layer evals** and supplies retrieval
ground truth through the feedback contract — only the agent knows whether the
returned evidence actually answered its question. Answer-level eval stacks
(RAGAS, Arize Phoenix) build *on top*, joined to Inherent via the `event_id`
returned on every search response. **Inherent never takes an LLM-as-judge
dependency.**

### Ground truth is mined from traffic, not authored

- **Golden set source: mine live traffic**, over requiring operators to author
  a labeled set (BYO) or generating questions with an LLM (synthetic). BYO-only
  eval products die at cold-start — most operators never write the golden set.
  Synthetic generation adds a generative-LLM dependency, non-determinism, and
  cost to v1.
- **Ground truth: the agent feedback loop**, over an offline LLM judge or an
  operator curation queue. The consuming agent reports back after using results
  (`answered` / `partial` / `not_relevant` + which chunks helped); positive
  feedback auto-promotes the captured query into a labeled eval case. This keeps
  the system deterministic and offline-capable, and makes the feedback API a
  product feature in its own right. The risk — agents must actually call it — is
  mitigated by making it a first-class MCP tool whose description instructs
  agents to report, and by shipping a trial labeling script (a human plays the
  agent's role through the same API) so the flywheel turns on day one before any
  agent integration exists.

### Deterministic scoring, in-process

- **Architecture: inside `inh-public-api-svc`**, over a dedicated eval service
  or an offline CLI harness. The eval engine needs the same index, auth, and
  tenancy the API already has; a separate deployable is cost the operator
  shouldn't pay until load demands it. A consumer-run harness can only test
  Inherent as a black box — it can't compute recall (it doesn't know the corpus)
  or replay at different configs.
- **Scoring is deterministic**: the ranking metrics (recall@k, MRR, nDCG) are
  dependency-free and computed in-process. v1 eval runs are **mode comparisons**
  (keyword vs. semantic vs. hybrid on the operator's corpus), which is the
  artifact that converts a trial ("recall@5: 0.91 hybrid vs 0.78 keyword, on
  *your* data"). Run-over-run regression tracking is deferred to v2 — it only
  becomes meaningful after weeks of history.

### Capture on by default, opt-out

- **Capture policy: on by default, per-tenant opt-out, bounded retention.**
  Opt-in capture leaves the feature silently empty in most deployments and looks
  broken. Capture is a fire-and-forget write-behind on the search path that can
  never fail or slow a search; raw query events purge after a configurable
  window (default 30 days) with an immediate-purge endpoint, while promoted eval
  cases persist until deleted. Because capture stores tenant query text — data
  Inherent did not previously persist — what is stored, where, and for how long
  is documented, and the opt-out and purge paths are first-class.

## Boundary: what this is not

- **Not answer/task evaluation.** Inherent scores retrieval, not whether the
  final answer was correct — it never sees the answer, the task, or the model.
  That eval belongs to the consumer and joins back via `event_id`.
- **Not an LLM judge.** No generative or judge-model dependency enters the
  serving or eval path. Operators who want LLM-graded or drift analysis point an
  external tool (e.g. Phoenix) at the `event_id`-joined data on their own
  infrastructure.
- **Not a second service.** The engine lives in the existing public API; a
  standing eval deployable was explicitly rejected.

## Consequences

- Inherent gains a trial-conversion capability: an operator proves retrieval
  quality on their own corpus in an afternoon, and re-runs it to catch
  regressions after re-chunks/upgrades — closing the gap the CI suite never
  addressed.
- Putting an eval surface **at the retrieval boundary** means regressions are
  caught where they originate instead of downstream as misattributed
  answer-quality problems.
- The feedback API becomes a durable contract between the consumer's judgment
  (which only it has) and the system that can act on it (which only Inherent
  is), and the same `event_id` lets richer external eval stacks compose on top
  without Inherent knowing anything about answers.
- New responsibility: capture persists tenant query text, so retention,
  opt-out, and cascade-on-tenant-deletion are ongoing product obligations, not
  afterthoughts.
- Deferred by design (v2+): run-over-run regression deltas and history;
  scheduled runs and alerting; a CLI/CI gate (thin client over the REST API);
  Phoenix dataset export and OTel/OpenInference instrumentation; synthetic
  question generation. These are additive and do not change the boundary above.
