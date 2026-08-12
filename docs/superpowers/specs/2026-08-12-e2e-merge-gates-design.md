# E2E Hardening + Merge Guardrails — Design

Date: 2026-08-12
Status: Approved (maintainer sign-off in session)
Owner: sagarmoy

## Problem

Change velocity has outgrown the safety net:

- The `main-protect` ruleset is **inert**: its ref pattern is the malformed single
  string `"refs/heads/main, release*"`, which matches no branch. GitHub's
  effective-rules API returns `[]` for both `main` and `dev`. PR #245 merged with
  zero reviews; CI green is currently decorative (no required status checks exist).
- Only ~13 of 1,840 tests are genuine E2E (live stack over the network), and none
  of them gate a PR — the compose integration lane runs post-merge/nightly only.
- `tests/e2e/` in `inh-public-api-svc` is fully mocked despite its name.
- Zero live E2E coverage of: the MCP server (all 14 tools, both transports),
  two-workspace tenancy isolation, document delete/refresh, binary formats
  (PDF/DOCX/XLSX) through the real pipeline, dead-letter recovery.
- The retrieval-eval hard gate is structurally noisy (#236): with 13 golden
  queries, the minimum detectable MRR move (0.5/13 ≈ 0.0385) exceeds the 0.02
  tolerance, so any single rank slip hard-fails the gate. Nightly integration
  failed ~5 of the last 7 runs; the ratchet auto-merge loop also stalls on
  `action_required` approvals.
- AGENTS.md says PRs target `dev`; 23 of the last 25 merged PRs targeted `main`.

## Decisions (maintainer-approved 2026-08-12)

1. **Branch model: `main` only.** `main` is the single protected integration
   branch; all PRs target it. `dev` is retired to scratch status. AGENTS.md
   updated to match.
2. **PR gate depth: tiered.** Fast checks + a new E2E smoke lane are required on
   every PR; the full integration + eval lane stays post-merge/nightly.
3. **E2E scope: all four gaps** — MCP live, tenancy isolation live,
   lifecycle (delete/refresh/binary formats), dead-letter recovery.
4. **Convention gates: all four** — CHANGELOG gate, eval-gate noise fix,
   CI hygiene pass, docs/ADR sync heuristic.

## Track A — Merge gates

### A1. Ruleset repair (`gh api`, admin action)

- Fix `ref_name.include` to `["refs/heads/main", "refs/heads/release*"]`.
- Add `required_status_checks` rule with the three contexts in A2.
- Keep: deletion block, non-fast-forward block, linear history, PR required,
  review-thread resolution.
- **Required approvals: 0** (sole-maintainer repo; GitHub forbids self-approval,
  so 1 would deadlock merges. The human gate = green required checks + a human
  pressing merge). Revisit to 1 when a second maintainer exists.
- **Drop required signed commits** (not current practice; would block all merges).

### A2. Required status checks (all always-running; never path-filtered)

1. **`Required tests before merge`** — existing `ci.yml` aggregate, hardened:
   - `concurrency` group with cancel-in-progress for PRs
   - `timeout-minutes` on every job
   - top-level least-privilege `permissions`
   - mypy + bandit extended to all three services (fix cheap findings, baseline
     the rest explicitly — scope explosion is not acceptable)
2. **`E2E smoke`** — new workflow, new `smoke` pytest marker (always combined
   with `compose`). Boots the compose stack (GHA-cached builds), runs:
   - upload→search roundtrip (text + one binary format)
   - MCP Streamable-HTTP live smoke (initialize, tools/list surface pin, search)
   - two-workspace tenancy smoke (cross-workspace search must be empty/blocked)
   - feedback `event_id` durability roundtrip
   Target ≤15 min wall clock; 40-min hard timeout. No benchmarks, no eval gate
   (eval noise must never block a PR).
3. **`Conventions`** — new job:
   - CHANGELOG gate: PR touching `services/**` must modify `[Unreleased]` in
     `CHANGELOG.md`; skip label `no-changelog`.
   - Docs-sync heuristic: PR touching API routers, the MCP `_TOOLS` registry, or
     `inh-contracts` public modules must touch `docs/**`; skip label
     `no-docs-needed`.
   - Pinned by root-level guard tests (same pattern as
     `tests/test_integration_workflow_guards.py`).

### A3. Post-merge lane (integration.yml) — role unchanged, gate fixed

- Eval-gate noise fix (#236): per-metric tolerance derived from corpus
  resolution — tolerance = max(configured floor, minimum detectable single-query
  delta given qrels size and metric). Implemented in `eval_gate.py`, unit-tested,
  documented in `docs/testing.md`, ADR 0003 amended.
- Dead-letter recovery E2E (B4) joins this lane, not smoke.

### A4. Docs

- AGENTS.md: PRs target `main`; gate table (what blocks merge, what runs
  post-merge); `dev` retired.
- `docs/testing.md`: smoke lane, `smoke` marker, tolerance derivation.

## Track B — New E2E suites (all `compose`-marked, real stack)

| # | Suite | Content | Lane |
|---|-------|---------|------|
| B1 | MCP live | Real MCP client. HTTP transport: initialize, tools/list pins the 10-tool HTTP surface (drift = failure), live `search_documents`, `upload_document`, `get_document`, `report_feedback`, `delete_document`. Stdio server exercised in-process against live backends; 14-tool stdio surface pinned. | smoke (HTTP core) + full |
| B2 | Tenancy isolation live | Bootstrap gains a second workspace + API key. Upload in A; prove search/read/delete from B is blocked/empty on both REST and MCP. | smoke (search) + full |
| B3 | Lifecycle | upload→delete→gone-from-search+404; refresh flow; PDF/DOCX/XLSX through the real pipeline (new binary fixtures under `docs/examples/sample-documents/`). The known live XLSX giant-chunk defect is pinned as an explicitly-marked expected-failure test with issue reference, so the fix PR flips it. | full (one binary format in smoke) |
| B4 | Dead-letter recovery | Stop a real dependency mid-ingestion (docker CLI from test host), assert row in `dead_letter_jobs`, restore dependency, replay via retry endpoint, assert processed + searchable. | full only |

Also: rename the fully-mocked `services/inh-public-api-svc/tests/e2e/` to
`tests/app_flows/` so the name stops lying; markers/config updated accordingly.

## Track C — Execution process

- Writers: Opus subagents, one per task, TDD (test first, watch it fail, then
  code), worktree isolation where tasks overlap files.
- Judge: main agent (Fable) adversarially reviews every diff against the
  discovery reports before commit.
- Repo-settings changes are executed directly by the main agent (approved above).
- Sequencing: A1/A2 hygiene + conventions → smoke lane + B1–B3 → B4 + eval fix →
  docs + final test report.
- Deliverable includes a test report: what ran, what passed, measured smoke-lane
  runtime, before/after gate table.
- Everything lands as reviewable commits on `feat/e2e-merge-gates` → PR to
  `main`, which becomes the first PR vetted by the new gates.

## Risks

- **Smoke-lane runtime is estimated, not measured.** Mitigations: GHA image
  cache, trimmed test subset. If >15 min after tuning, options are prebuilt
  images or moving the binary-format case to the full lane; the lane stays
  required either way unless it exceeds ~20 min.
- **mypy/bandit on ingestion/contracts may surface a backlog.** Fix cheap
  findings; baseline the rest with explicit per-file ignores + a tracking issue.
- **Ruleset edits are admin-scoped.** If the token lacks admin, fall back to
  printing the exact `gh api` calls for the maintainer to run.
