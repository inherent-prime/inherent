---
search:
  exclude: true
---

# Engineering Learnings

Durable lessons from defects that survived to `main`. CLAUDE.md references
this file — read the matching entry before touching the related area. Add an
entry when a shipped defect teaches something a rule alone can't carry: one
entry per root cause, newest first.

## #110 — A fixed workflow id turns a routine race into a ~10-minute stall, and terminating a workflow doesn't stop its work (2026-08-06)

**Defect (round 1).** `DocumentIngestionWorkflow` is started with a
deterministic id (`ingest-{document_id}`,
`services/inh-ingestion-svc/src/temporal/trigger.py`) so a workflow can be
addressed for status queries by document_id alone. That determinism is also
a collision surface: re-indexing a document (edited-content re-upload, or
the `/refresh` endpoint under load) while the prior run for the same
document_id was still open hit Temporal's default `id_conflict_policy`
(`UNSPECIFIED`), which raises `WorkflowAlreadyStartedError` on a same-id
collision against a running execution. That exception propagated out of the
MQ handler, so `RedisMQService` never ACKed the message — and every
redelivery collided with the *same* still-open run and failed again, so the
message effectively wasn't retried on a fixed cadence at all: it waited out
however long the stale run took to close on its own (~10min in the CI run
that surfaced it).

**Defect (round 2 — independent review caught what round 1 missed).** The
round-1 fix (`id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING`
on the MQ path) resolves the collision but was shipped with four gaps, all
from the same wrong assumption: that terminating a Temporal WORKFLOW stops
its work.

- **It doesn't.** `grep -rn heartbeat src/` returns nothing — no activity in
  this service heartbeats, and no `cancellation_type` is set on any
  `execute_activity` call. Temporal only interrupts a running activity via a
  heartbeat round-trip; termination closes the workflow execution
  server-side and never delivers another workflow task, so an
  already-dispatched `store_in_postgresql` / `store_in_weaviate` from the
  terminated (superseded) run keeps running on the worker, unaware, and its
  eventual write can land AFTER the newer run already committed — silently
  reverting the document to stale content while reporting
  `status='processed'`. Fixed with a fencing token
  (`processed_documents.active_run_id`, migration 016): every run claims the
  document as its first action, and the store activities only commit when
  the claim still matches the run doing the write.
- **The staging-cleanup justification for the above was itself false, and
  would have shipped as a citation, not a check.** The PR claimed orphaned
  staging rows were "covered by a 1-hour safety net"
  (`StagingService.cleanup_stale`). True that it filters on a 1-hour age —
  false that it runs periodically: it's called exactly twice in
  `worker.py`, both at worker STARTUP. Pre-#110 that was fine (staging could
  only orphan on a worker crash, which implies a restart, which re-triggers
  the sweep). Post-#110, termination is a ROUTINE event on an
  otherwise-healthy worker that never restarts, and termination skips the
  workflow's own `finally: cleanup_staging` (termination doesn't run
  workflow code at all — unlike cancellation, which the workflow's own
  try/except/finally CAN observe). So every superseded re-index now orphans
  a row with no compensating cleanup until the worker happens to restart.
  Fixed with an actual periodic task (`_periodic_staging_cleanup`, 15 min).
  Lesson: a citation to "existing infrastructure already handles this" is a
  claim, not a check — read the code path, don't infer it from a comment
  or a plausible-sounding name like "safety net."
- **The pattern sweep checked the wrong axis.** Round 1 enumerated other
  `start_workflow` call SITES sharing the fixed-id shape. The right axis for
  a change to a shared METHOD (`trigger_workflow_async`) is its CALLERS —
  there were two (`src/main.py`'s MQ handler and `src/api/app.py`'s
  dead-letter retry), and the second was missed. A dead-letter retry replays
  a payload that already failed once — possibly long ago, possibly
  superseded by a since-corrected upload — so it must NOT get the same
  supersede-on-collision behavior a fresh upload/refresh event should.
  Fixed by making the conflict policy a per-call parameter
  (`supersede_running`), not a shared module constant.
- **A behavior change to a shared method changes what its OTHER callers can
  raise.** `POST /ingest?wait=true` blocks on `handle.result()`; before
  #110 that could never raise in practice (the workflow catches its own
  exceptions and returns a normal result), so nothing wrapped it. After
  #110, an unrelated concurrent MQ refresh can terminate that exact run out
  from under the waiting caller, and `handle.result()` now raises
  `WorkflowFailureError(cause=TerminatedError)` — an unhandled 500 without
  this fix. When a change makes an exception newly reachable somewhere, grep
  isn't enough to find where — trace every caller of what changed, including
  ones several files away that don't obviously relate to the change's own
  described scope.

**Learnings.**

- A deterministic Temporal workflow id is a deliberate collision surface, not
  an incidental one — it exists so callers/queries can address a run without
  tracking a run id. Every `start_workflow` call using one needs an explicit
  decision about `id_conflict_policy`, made at the call site, not inherited
  silently from the SDK default (`UNSPECIFIED` raises). Grep for `id=f"` next
  to `start_workflow(` when auditing this pattern — the fixed-id shape is
  visible in the string itself.
- The right conflict policy depends on what a fresh request *means*: a
  synchronous duplicate request (accidental double-click) should be rejected
  fast (409) so the caller doesn't do double work — that's what `/ingest` and
  the chunk-edit endpoint already did correctly, and what dead-letter retry
  needed too (round 2). A re-index/refresh is different: the fresh event *is*
  the newer truth, so it should supersede a stale in-flight run
  (`WorkflowIDConflictPolicy.TERMINATE_EXISTING`), not queue behind it or
  bounce off it as a conflict. Naming the same exception doesn't mean the
  same handling is correct everywhere it's caught — and it isn't even always
  the same handling for the same METHOD, once that method has more than one
  caller with different intents.
- **`TERMINATE_EXISTING` (or terminating a workflow at all) is not a
  cancellation primitive** — it stops the workflow's orchestration, not any
  activity already dispatched. Anything that relies on "terminating the
  workflow stops its side effects" needs either (a) activity
  heartbeating + `cancellation_type` wired so termination can actually reach
  in-flight work, or (b) an application-level fencing token so the SIDE
  EFFECT (the write), not the orchestration, is what refuses stale work.
  This codebase chose (b) — cheaper to reason about correctly than wiring
  heartbeats through every activity, and it doesn't depend on Temporal's
  cancellation delivery timing.
- A raised exception on an MQ consumer path doesn't fail fast by default — it
  fails on whatever cadence the queue's redelivery/reclaim logic happens to
  produce, which can be far slower and less regular than the nominal retry
  interval suggests (see #179, filed alongside this fix: the reclaim pass
  itself only runs when *unrelated* new traffic also arrives on the stream).
  When a stall shows up as "roughly N minutes" in an incident, distrust that
  as a constant until you've traced the actual mechanism producing it — here
  it was "however long the other workflow run happens to take," not a timeout.
- Judge/UAT review is not redundant with tests-green. UAT accepted this fix;
  an independent judge caught four blockers a persona-driven walkthrough
  didn't, by asking "what does termination actually stop?" and "is this
  claim about existing infrastructure actually true?" — both questions a
  passing test suite doesn't ask on its own.

**Defect (round 3 — fencing the write is not enough if the CLAIM isn't
ordered).** Round 2's fencing token stopped a stale write from landing, but
the CLAIM step that sets `active_run_id` was itself a bare unconditional
UPDATE with no ordering predicate — whichever transaction committed LAST
owned the document, regardless of which run actually STARTED later. Concrete
inversion this allowed, in exactly the retry window #110 exists to serve: run
A starts, its claim activity is dispatched; ~50ms later a fresh run B
terminates A and starts. Termination doesn't stop A's already-dispatched
claim activity (same premise the store-side fence already accepts). If A's
claim commits AFTER B's, the DEAD run A ends up owning `active_run_id`, and
B — the legitimate, newest run — gets fenced out of its OWN store step. The
newest content that's supposed to "win immediately" instead hard-fails and
gets dead-lettered. Fixed by ordering the claim on each run's Temporal
**start time** (`workflow.info().start_time`, deterministic and
workflow-supplied) rather than commit order: `active_run_claimed_at`
(migration 017), guarded with "unclaimed OR existing claim started at or
before mine."

- **A fence that protects the WRITE still needs an ordered CLAIM, or the
  claim itself becomes the race.** Fencing tokens are usually presented as a
  single mechanism ("check a token before writing"), but there are actually
  two operations that both need correctness: acquiring/updating the token
  (the claim) and checking it (the write guard). Round 2 got the second half
  right and missed that the first half was just as exposed to the same
  "termination doesn't stop in-flight activities" premise the whole fix is
  built on. When you fence step N of a workflow, ask whether step N-1 (or
  any earlier step) needs the same treatment — a fence that's airtight
  everywhere except where the token itself is written is not airtight.
- **Prefer a domain timestamp Temporal already gives you over inventing an
  ordering scheme.** `workflow.info().start_time` was sitting right there,
  deterministic and safe inside `@workflow.run` (unlike `datetime.now()`,
  which would break workflow replay). No new coordination mechanism, no
  clock synchronization concerns beyond what Temporal's server already
  guarantees for workflow metadata.

## #146 — A single-probe readiness wait races the rest of the corpus it's gating (2026-07-24)

**Defect.** `test_compose_retrieval_regression.py`'s corpus-readiness wait
checked exactly ONE query ("wait until the corpus is searchable" == wait
until the *first* query in the golden corpus dict returns *any* result),
then immediately scored every other query in the corpus. On a slow/CPU-only
embedding runner, the probe's own document can finish extract->chunk->embed
->index well before the rest of the corpus does — so scoring started while
most documents were still mid-pipeline, and every query backed by a
not-yet-indexed document legitimately scored zero, not because ranking was
bad but because there was nothing there yet to rank. This had already
shipped: the committed `corpus/retrieval_history.jsonl`'s first entry (sha
`201363a`, the real baseline this branch's Phase 1 work seeded from) shows
the signature exactly — `exact_id`/`stale_version` (3 small fixtures) scored
a perfect 1.0 while `general`/`paraphrase` (the bulk of the corpus,
competing for the same embedding-worker time) scored near zero, in every
mode uniformly. The same race was independently observed live during #146
development: a freshly-added query scored 0/0/0 on the first run and a
perfect 1.0/1.0/1.0 on an immediate re-run with no code change in between.
It surfaced via a `/cross-review` pass questioning why the committed
baseline jumped ~4x between two history entries, not from anyone noticing
the original low number was wrong at the time it was recorded.

**Learnings.**

- A "wait until ready" check that only probes ONE representative item is a
  race whenever readiness isn't atomic across the whole set — it proves
  *a* document is ready, not *the* document a given assertion needs. If N
  independent things must all be ready before an assertion runs, the wait
  must check all N (or the specific ones each assertion depends on), not
  one stand-in for all of them.
- A suspiciously uniform low score across every mode/metric in an eval
  report (here: near-identical `recall@5`/`mrr`/`ndcg@5` around 0.05-0.21 for
  every search mode) is a signature worth checking against harness
  correctness before accepting it as a real quality measurement, especially
  when a subset of categories score perfectly and the rest score near zero —
  that split usually means "some things weren't there yet," not "ranking is
  uniformly bad here but great there."
- A number silently seeded into a governance gate (a baseline, a threshold)
  from one run is only as trustworthy as the harness that produced it; a
  harness bug upstream of the metric computation doesn't fail loud — it just
  produces a technically-valid but wrong number that then gets ratcheted on.

**Mandatory pattern.** Any live-stack eval/benchmark that waits for
"corpus ready" before scoring must check readiness per-query (or per
fixture) against what THAT query specifically depends on, not a single
probe for the whole corpus — see `test_compose_retrieval_regression.py`'s
`_query_ready` (requires each query's own judged-relevant document, or any
result for a by-design abstention query, before including it in scoring).

## #139 — A CI job that pushes to a protected branch fails silently, forever (2026-07-23)

**Defect.** `eval-baseline-ratchet` computed a correct ratcheted baseline on
every green `main` run, then tried to `git push origin HEAD:main`. Branch
protection on `main` requires a status check the direct push can never
satisfy, so every attempt failed with `remote rejected (protected branch hook
declined)` — for every run since the job shipped. The job's own retry loop
(5 attempts, re-fetch and reset each time) made this look like transient
contention, but the failure was structural, not a race: nothing about
retrying changes what branch protection rejects. `corpus/retrieval_baseline.json`
stayed at its seeded zeros the whole time, so the relative regression gate
was a no-op — only the absolute `RETRIEVAL_MIN_RECALL5` floor was ever live.
The job did fail loudly in CI (by design, per its own comment), but a
red job on a job nobody expects to matter for merge (`eval-baseline-ratchet`
runs post-merge on `main`/nightly, never blocks a PR) reads as background
noise, not an incident — it went unnoticed until an unrelated eval-hardening
pass re-derived the baseline from source and diffed it against zeros.

**Learnings.**

- A CI job that writes back to a protected branch needs a push path that
  branch protection actually allows — a PAT/GitHub App token with a bypass,
  or (what this fix uses) open a PR and let the normal required check run,
  rather than pushing directly and hoping protection doesn't apply to bots.
  Retrying a rejected push is never the fix; branch protection isn't a race
  condition.
  A red job whose failure mode is "runs post-merge, never blocks anything"
  needs an explicit owner/alert, or a structural failure hides for as long as
  the job keeps quietly failing in the same way. If a CI job's only visible
  effect of failing is a red run nobody is looking at, that job's *success*
  needs to be verified once, deliberately — not assumed from "the step
  exists and doesn't crash."
- The PR-based fix has its own edge cases, caught only by cross-model review
  (`/cross-review`) before this ever ran in CI, not by writing the code
  carefully the first time: `gh pr view <branch>` matches an already-merged
  PR on a reused branch just as readily as an open one, so checking for "a PR
  exists" instead of "an *open* PR exists" reintroduces the exact same
  silent-stop failure one layer up, after exactly one successful merge.
  Reusing a branch across runs also means a not-yet-merged run's state can be
  silently discarded by the next run if that next run rebuilds from `main`
  instead of from the open PR's own tip. And `--force-with-lease` protects
  nothing if the remote ref it leases against was never fetched — the push
  it's meant to guard just gets rejected as stale instead. None of these are
  exotic: they are the default behavior of `gh pr view`, `git checkout -B
  <branch> origin/main`, and `git push --force-with-lease` respectively: each
  needed exercising against the "PR already exists" and "PR still open" cases
  specifically, not just the first-run case.
- The default `GITHUB_TOKEN` cannot be used to make a CI job's *own* fix
  self-verifying: GitHub explicitly excludes pushes/PRs made with it from
  triggering other workflow runs, so a job that opens a PR with `github.token`
  and expects `ci.yml` to pick it up will never see that check fire. A
  same-repo elevated token (PAT or GitHub App installation token, added as a
  scoped secret) is required for a CI-authored PR to trigger a required
  check by itself; this is a permissions/trust boundary GitHub enforces on
  purpose, not a bug to route around.

**Mandatory pattern.** Any workflow job that writes generated/ratcheted state
back to `main` (baseline files, changelogs, lockfiles) must do so via a PR +
optional auto-merge (see `eval-baseline-ratchet` in
`.github/workflows/integration.yml`), never via `git push origin HEAD:<protected-branch>`.
That PR path must (a) check for an *open* PR specifically before deciding
whether to create one, (b) fetch the reused branch before force-pushing to it
and before deciding whether to reset vs. pull its state forward, and (c) use
an elevated token (not the default `GITHUB_TOKEN`) if the PR is expected to
trigger its own required check without human intervention.

## #112 — Writing release notes is not the same as publishing them (2026-07-13)

**Defect.** `v0.5.0` shipped with a well-written annotated tag message and a
matching `CHANGELOG.md` entry, but neither is where a consumer looks first.
No tag — `v0.1.0`, `v0.4.1`, `v0.5.0` — had ever been published as a GitHub
Release, so the Releases tab was empty. Separately, the GHCR package page for
both images showed "No description provided": `publish.yml`'s "Build and
push" step passed `labels: ${{ steps.meta.outputs.labels }}` but not
`annotations:`, and for a multi-platform build GHCR's package UI reads OCI
annotations on the manifest index, not labels baked into each per-arch image
config — so the metadata `docker/metadata-action` generated never reached the
registry UI.

**Learnings.**

- Writing content and publishing it to the surface people actually check are
  two different steps. A checklist item that says "summarize changes" is
  satisfied by content existing *somewhere*; it needs to name the destination
  (Releases tab, package page) or it will be satisfied by content nobody
  finds.
- Multi-platform image metadata has two independent channels — `labels`
  (per-arch image config) and `annotations` (manifest index) — and a registry
  UI may read only one of them. When a build-push-action step consumes a
  `metadata-action` output for one, check whether it should also consume the
  other.

**Mandatory pattern.** `docs/maintainers/releasing.md`'s checklist has an
explicit "publish a GitHub Release from the tag" step; do not consider a
release's notes done until that Release exists. `publish.yml`'s "Build and
push" step passes both `labels:` and `annotations:` from `steps.meta.outputs`
for both services — do not drop `annotations:` when touching that step.

## #99 — A compensating write is itself a fallible step (2026-07-12)

**Defect.** `intake_document` marked a document `failed` after an MQ publish
failure, but wrapped the mark in log-and-swallow. When the DB also blipped,
the row stayed `pending` while the client saw `failed` — an orphan no
recovery process could find. The pattern sweep found the identical swallow at
all three compensation sites: upload intake (shared REST + MCP), REST
refresh, MCP refresh.

**Learnings.**

- Compensation code runs exactly when infrastructure is already failing, so
  it is the code *most* likely to fail. It needs retry and loud exhaustion —
  more care than the happy path, not less.
- A state write inside an `except` block that is itself wrapped in
  `try/except`-log is the signature of this defect. Grep for it in review.
- An `xfail` test pins a missing contract but masks its own rot: the #99
  xfail hid a stale patch target (the #87 refactor moved
  `get_storage_service` out of the module the test patched), so the test was
  failing for the wrong reason and nobody saw. When removing an `xfail`
  marker, first prove the test fails for the documented reason.

**Mandatory pattern.** Route every compensating mark through
`services/inh-public-api-svc/src/services/compensation.py::mark_document_failed_with_retry`
— 3 attempts, exponential backoff; exhaustion emits a CRITICAL log
(document_id + workspace_id for reconciliation) and bumps
`document_compensation_exhausted_total{operation}`. Never call
`database.mark_document_failed` bare inside an `except` block. The contract
lives in `tests/contract/test_failure_parity.py` (upload + refresh, both
surfaces).

**Alerting.** Alert on any increase of
`document_compensation_exhausted_total`. Each increase is one document
orphaned as `pending` that needs manual reconciliation via the paired
CRITICAL log line.
