# E2E Hardening + Merge Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every merge to `main` gated by fast checks + a real E2E smoke lane, add live E2E coverage for MCP/tenancy/lifecycle/dead-letter flows, and fix the eval-gate's structural noise.

**Architecture:** Three required PR checks (`Required tests before merge`, `E2E smoke`, `Conventions`) enforced by a repaired GitHub ruleset on `main`. New compose-backed E2E suites follow the existing `tests/integration/test_compose_integration.py` pattern (httpx + env-var config + skip-if-no-stack). The heavy lane (`integration.yml`) keeps its post-merge role and absorbs the new full-lane tests automatically via the `compose` marker.

**Tech Stack:** pytest 9 (`compose`/new `smoke` markers), httpx, `mcp` Python SDK (>=1.1.2, already a dependency), docker compose, GitHub Actions, GitHub rulesets via `gh api`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-e2e-merge-gates-design.md` — read it first.
- All new live tests: marker `compose` (+ `smoke` only if in the smoke lane), skip (not fail) when stack unreachable, config via `PUBLIC_API_URL` / `INTEGRATION_API_KEY` / `INTEGRATION_WORKSPACE_ID` / `INTEGRATION_TIMEOUT` env vars with the same defaults as `test_compose_integration.py`.
- The eval gate (`eval_gate` marker) must NEVER be selected by the smoke lane.
- Every behavior change gets a `CHANGELOG.md` `[Unreleased]` entry (repo rule).
- TDD: write the failing test, watch it fail, implement, watch it pass, commit. For workflow YAML (not unit-testable), the "test" is the root-level guard test + `actionlint`/manual `gh` verification.
- Follow repo lint/format: `ruff`, `black`. Run `make lint format` equivalents per service before commit.
- Commits: conventional style, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Do not touch `retrieval_baseline.json` values (only its `_comment` if the tolerance semantics doc requires it).

## File Structure (created/modified)

```
.github/workflows/ci.yml                  # M: concurrency, timeouts, permissions, mypy/bandit matrix
.github/workflows/conventions.yml         # C: CHANGELOG + docs-sync gates
.github/workflows/e2e-smoke.yml           # C: compose smoke lane
tests/test_conventions_workflow_guards.py # C: pins conventions workflow invariants
tests/test_e2e_smoke_workflow_guards.py   # C: pins smoke-lane invariants (no eval_gate, timeout, triggers)
scripts/dev/bootstrap.sh                  # M: second workspace/key for tenancy tests
services/inh-public-api-svc/pyproject.toml            # M: smoke marker
services/inh-ingestion-svc/pyproject.toml             # M: smoke marker
services/inh-public-api-svc/tests/integration/test_compose_integration.py  # M: tag smoke subset
services/inh-public-api-svc/tests/integration/test_compose_mcp.py          # C: B1
services/inh-public-api-svc/tests/integration/test_compose_tenancy.py      # C: B2
services/inh-public-api-svc/tests/integration/test_compose_lifecycle.py    # C: B3
services/inh-public-api-svc/tests/integration/test_compose_event_durability.py # C: smoke feedback roundtrip
services/inh-ingestion-svc/tests/failure_injection/test_compose_dead_letter_recovery.py # C: B4
services/inh-public-api-svc/tests/evals/eval_gate.py          # M: derived tolerance
services/inh-public-api-svc/tests/evals/test_eval_gate.py     # M: tolerance derivation tests
services/inh-public-api-svc/tests/e2e/ -> tests/app_flows/    # RENAME (B-extra)
docs/examples/sample-documents/sample.pdf|.docx|.xlsx         # C: binary fixtures
docs/testing.md, docs/adr/0003-*.md, AGENTS.md, CHANGELOG.md  # M: docs
```

Task order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 (ruleset, main agent) → 11 (docs) → 12 (verify + report). Tasks 4–7 depend on 3 (smoke marker exists). Tasks 5–7 are independent of each other after 4.

---

### Task 1: CI hygiene (`ci.yml`)

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: job names must remain exactly `inh-ingestion-svc checks` / `inh-public-api-svc checks` / `inh-contracts checks` / `root tests/ (repo-level pins)` / `Required tests before merge` (Task 10 registers `Required tests before merge` as a required check; renaming breaks the gate).

- [ ] **Step 1:** Read `.github/workflows/ci.yml` fully. Add at top level:

```yaml
permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

- [ ] **Step 2:** Add `timeout-minutes: 30` to `service-checks` and `required-tests`, `timeout-minutes: 10` to `root-tests`.
- [ ] **Step 3:** Extend mypy/bandit to all services: in the matrix, set the `typecheck`/`security` entries for `inh-ingestion-svc` and `inh-contracts` to run `uv run mypy src` and `uv run bandit -r src -ll` (mirror the public-api invocations exactly, including any config flags used there). Check each service's `pyproject.toml` for existing `[tool.mypy]` config; add a minimal one (same strictness the public-api uses) if absent.
- [ ] **Step 4:** Run mypy + bandit locally for ingestion and contracts. Triage: fix cheap findings (unused ignores, missing return types on touched files); for a pre-existing backlog, add targeted per-module overrides in that service's `[tool.mypy]` (e.g. `disallow_untyped_defs = false` for legacy modules) with a comment referencing a new tracking issue — create the issue with `gh issue create --title "mypy/bandit baseline debt: <service>" --body <summary>`. CI must pass green, but never by disabling the check wholesale.
- [ ] **Step 5:** Validate: `uvx actionlint .github/workflows/ci.yml` (if actionlint unavailable, `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`). Run the full local equivalent: per-service `uv run ruff check . && uv run black --check . && uv run mypy src && uv run pytest` for the two newly-covered services.
- [ ] **Step 6:** Commit: `ci: harden ci.yml (permissions, concurrency, timeouts, full mypy/bandit matrix)`.

### Task 2: Conventions gate

**Files:**
- Create: `.github/workflows/conventions.yml`
- Create: `tests/test_conventions_workflow_guards.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: workflow name `Conventions`, single job with **name exactly `Conventions`** (required-check context for Task 10). Skip labels: `no-changelog`, `no-docs-needed`.

- [ ] **Step 1:** Write the failing guard test at repo root (pattern-copy `tests/test_integration_workflow_guards.py` — regex assertions over the YAML text). Assert: workflow exists; `pull_request` trigger includes types `opened, synchronize, reopened, labeled, unlabeled`; job checks out with `fetch-depth: 0`; the CHANGELOG check greps the PR label list for `no-changelog`; the docs check greps for `no-docs-needed`; `timeout-minutes` present.
- [ ] **Step 2:** Run `uvx 'pytest==9.0.2' tests/test_conventions_workflow_guards.py -v` → FAIL (file missing).
- [ ] **Step 3:** Write `.github/workflows/conventions.yml`:

```yaml
name: Conventions
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: conventions-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  conventions:
    name: Conventions
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Collect changed files
        id: diff
        run: |
          git diff --name-only "origin/${{ github.base_ref }}...HEAD" > changed.txt
          echo "Changed files:" && cat changed.txt
        env: {}
      - name: CHANGELOG gate
        if: ${{ !contains(join(github.event.pull_request.labels.*.name, ','), 'no-changelog') }}
        run: |
          if grep -qE '^services/' changed.txt && ! grep -qx 'CHANGELOG.md' changed.txt; then
            echo "::error::PR touches services/** but has no CHANGELOG.md entry. Add one under [Unreleased] or apply the 'no-changelog' label."
            exit 1
          fi
      - name: Docs-sync gate
        if: ${{ !contains(join(github.event.pull_request.labels.*.name, ','), 'no-docs-needed') }}
        run: |
          if grep -qE '^services/inh-public-api-svc/src/(api/v1/|mcp_server/server\.py)|^services/inh-contracts/src/' changed.txt \
             && ! grep -qE '^docs/' changed.txt; then
            echo "::error::PR changes API routers / MCP tool registry / shared contracts but touches no docs/. Update docs or apply the 'no-docs-needed' label."
            exit 1
          fi
```

(Adjust the checkout+diff base handling if `origin/<base>` is unavailable: `git fetch origin ${{ github.base_ref }}` first.)
- [ ] **Step 4:** Create both labels: `gh label create no-changelog --description "Skip CHANGELOG gate" --color EDEDED; gh label create no-docs-needed --description "Skip docs-sync gate" --color EDEDED` (ignore already-exists errors).
- [ ] **Step 5:** Run the guard test → PASS. Run `uvx actionlint .github/workflows/conventions.yml`.
- [ ] **Step 6:** Add CHANGELOG `[Unreleased]` entry (this plan's work gets one entry, extended per task: "CI: required merge gates — hardened CI, Conventions gate, E2E smoke lane; new live E2E suites (MCP, tenancy, lifecycle, dead-letter); eval-gate tolerance derived from corpus resolution").
- [ ] **Step 7:** Commit: `ci: add Conventions gate (CHANGELOG + docs-sync) with guard tests`.

### Task 3: `smoke` marker + E2E smoke workflow

**Files:**
- Modify: `services/inh-public-api-svc/pyproject.toml`, `services/inh-ingestion-svc/pyproject.toml` (markers list)
- Modify: `services/inh-public-api-svc/tests/integration/test_compose_integration.py`
- Create: `.github/workflows/e2e-smoke.yml`
- Create: `tests/test_e2e_smoke_workflow_guards.py`

**Interfaces:**
- Produces: marker `smoke: PR-blocking E2E smoke subset (always combined with compose)`; workflow `E2E smoke`, job name exactly `E2E smoke`; smoke selection expression `-m "smoke and compose"`. Tasks 4–6 tag their smoke-lane tests with `@pytest.mark.smoke`.

- [ ] **Step 1:** Write failing root guard test asserting about `.github/workflows/e2e-smoke.yml`: exists; triggers on `pull_request`; runs `pytest` with `-m "smoke and compose"` (and NOT any expression selecting `eval_gate`); has `timeout-minutes: 40`; has a concurrency group with `cancel-in-progress: true`; boots the stack with `docker compose up` and `--wait`. Also assert both service `pyproject.toml` files declare the `smoke` marker. Run → FAIL.
- [ ] **Step 2:** Add the `smoke` marker line to both services' `markers = [...]`.
- [ ] **Step 3:** Tag the smoke subset in `test_compose_integration.py`: add `@pytest.mark.smoke` to `test_ingestion_to_search_roundtrip` ONLY (module-level pytestmark stays as-is; the other roundtrip variants stay full-lane).
- [ ] **Step 4:** Write `.github/workflows/e2e-smoke.yml`. Copy the stack-boot/bootstrap/teardown steps from `integration.yml`'s `compose-integration` job verbatim (checkout, compose up --build --wait, health wait, `make bootstrap`, failure-path log dump, `docker compose down -v`), then the test steps:

```yaml
name: E2E smoke
on:
  pull_request:
permissions:
  contents: read
concurrency:
  group: e2e-smoke-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
jobs:
  e2e-smoke:
    name: E2E smoke
    runs-on: ubuntu-latest
    timeout-minutes: 40
    env:
      INTEGRATION_TIMEOUT: "600"
    steps:
      # ... boot steps copied from integration.yml ...
      - name: Public API smoke tests
        working-directory: services/inh-public-api-svc
        run: uv run pytest -m "smoke and compose" -v --no-cov
      - name: Ingestion smoke tests
        working-directory: services/inh-ingestion-svc
        run: uv run pytest -m "smoke and compose" -v --no-cov
      # ... failure log dump + docker compose down -v (always) ...
```

Match `integration.yml`'s env (`RATE_LIMIT_ENABLED: "false"` etc.) exactly so smoke and full lanes test the same stack shape. Use `--no-cov` (coverage is the fast lane's job; it dominates runtime).
- [ ] **Step 5:** Run guard tests → PASS. `uvx actionlint .github/workflows/e2e-smoke.yml`.
- [ ] **Step 6:** Local proof: `make quickstart` (stack up), then `cd services/inh-public-api-svc && uv run pytest -m "smoke and compose" -v --no-cov` → the tagged roundtrip test passes; record wall-clock time.
- [ ] **Step 7:** Commit: `ci: add E2E smoke lane (smoke marker + pull_request compose workflow)`.

### Task 4: MCP live E2E (B1)

**Files:**
- Create: `services/inh-public-api-svc/tests/integration/test_compose_mcp.py`

**Interfaces:**
- Consumes: `smoke` marker (Task 3); live stack env-var pattern.
- Produces: `mcp_http_session` fixture pattern that Task 5 copies for MCP-side tenancy checks.

- [ ] **Step 1:** Read `src/mcp_server/server.py` (the `_TOOLS` registry and `ToolDef.http_exposed`), `src/mcp_server/http_transport.py` (mount path, auth header handling), and `tests/contract/test_mcp_http_transport.py` (how the HTTP surface is asserted offline). Derive the canonical expected tool lists **from the registry itself is NOT allowed** — hardcode the two lists in the test (14 stdio names, 10 HTTP names) so silent registry drift fails the test, with a comment saying exactly that.
- [ ] **Step 2:** Write the test file, `pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]`, same env/config/skip helpers as `test_compose_integration.py`. Tests (write all first — they fail against a running stack only if behavior is broken; the failing-first cycle here is "run before implementing any needed test helpers"):
  - `test_http_tools_list_pins_exposed_surface` (`@pytest.mark.smoke`): open an MCP Streamable-HTTP session against `{API_URL}/mcp` using the `mcp` SDK (`mcp.client.streamable_http.streamablehttp_client`, auth via the same `X-API-Key` header dict the transport expects — confirm exact header from `http_transport.py`); `initialize`; `tools/list`; assert sorted tool names == the hardcoded 10.
  - `test_http_search_documents_roundtrip` (`@pytest.mark.smoke`): upload a doc via REST (reuse the upload+poll helper pattern), then `call_tool("search_documents", {...})` over HTTP; assert the uploaded content is cited in the result payload.
  - `test_http_upload_get_delete_document_roundtrip`: `call_tool("upload_document", ...)` → poll `get_document` until processed → `delete_document` → `get_document` reports not-found error shape.
  - `test_stdio_surface_and_search`: construct the stdio server in-process (import the server module, use the SDK's in-memory client-server streams — `mcp.shared.memory.create_connected_server_and_client_session` if available in the pinned SDK, else spawn `python -m src.mcp_server` as a subprocess with env pointing at the compose-published backends); assert 14 tool names; `call_tool("search_documents", ...)` returns the doc uploaded earlier.
  - `test_http_report_feedback_closes_loop`: search over MCP HTTP → take `event_id` from the response → REST `POST /v1/feedback` with it → 2xx (proves MCP-captured events are durable, the #240 seam).
- [ ] **Step 3:** Stack up (`make quickstart` if not running). Run: `uv run pytest tests/integration/test_compose_mcp.py -v --no-cov`. Iterate until green — genuine product bugs found here are REPORTED in the task summary, not silently worked around (an xfail with an issue reference is the only acceptable encoding of a real bug).
- [ ] **Step 4:** Lint/format; run the offline default suite (`uv run pytest --no-cov -q`) to prove no collection breakage.
- [ ] **Step 5:** Commit: `test: add live MCP E2E suite (HTTP + stdio, surface pins, search/upload/feedback)`.

### Task 5: Tenancy isolation live E2E (B2)

**Files:**
- Modify: `scripts/dev/bootstrap.sh`
- Create: `services/inh-public-api-svc/tests/integration/test_compose_tenancy.py`

**Interfaces:**
- Consumes: `mcp_http_session` pattern from Task 4.
- Produces: second seeded principal — key `ink_dev_local_key_002`, workspace `ws_local_002`, user `user_local_002` — available to any compose test via env vars `INTEGRATION_API_KEY_B` / `INTEGRATION_WORKSPACE_ID_B` (defaults in-test).

- [ ] **Step 1:** Extend `bootstrap.sh`: refactor the existing PG-insert + Mongo-upsert into a `seed_principal <key> <key_id> <user_id> <workspace_id> <name>` function; call it for the existing 001 principal and the new 002 principal (same permission set). Keep idempotent (`ON CONFLICT` / `updateOne upsert` already handle it). Run `make bootstrap` against the live stack; verify both keys work via `curl -s -H "X-API-Key: ..." $API/v1/documents`.
- [ ] **Step 2:** Write `test_compose_tenancy.py` (compose/integration/slow marks). Module fixture uploads a uniquely-named doc with unique content sentinel (uuid in text) as principal A. Tests:
  - `test_cross_workspace_search_is_empty` (`@pytest.mark.smoke`): search for the sentinel as B (B's key + B's workspace header) → zero hits mentioning the sentinel. Also search as B while sending **A's workspace id header with B's key** → expect 403/404 (assert the actual documented status from `docs/access-control.md`, not a guess).
  - `test_cross_workspace_document_read_blocked`: `GET /v1/documents/{A_doc_id}` and `GET /v1/chunks/{A_doc_id}` as B → 404 (not 403 leaking existence — assert per access-control doc).
  - `test_cross_workspace_delete_blocked`: `DELETE /v1/documents/{A_doc_id}` as B → 404; then as A, `GET` still 200 (doc unharmed).
  - `test_mcp_cross_workspace_search_is_empty`: MCP HTTP session with B's credentials; `search_documents` for the sentinel → no hits.
- [ ] **Step 3:** Run against live stack → green (report any real isolation failure IMMEDIATELY as a critical finding, do not commit a workaround).
- [ ] **Step 4:** Lint/format; offline suite collection check; commit: `test: add live two-workspace tenancy isolation E2E (REST + MCP)`.

### Task 6: Lifecycle + binary formats E2E (B3)

**Files:**
- Create: `services/inh-public-api-svc/tests/integration/test_compose_lifecycle.py`
- Create: `services/inh-public-api-svc/tests/integration/test_compose_event_durability.py`
- Create: `docs/examples/sample-documents/sample.pdf`, `sample.docx`, `sample.xlsx`

**Interfaces:**
- Consumes: upload/poll helpers pattern; `smoke` marker.

- [ ] **Step 1:** Generate the three binary fixtures with a scratch script (reportlab or fpdf for PDF; `python-docx`; `openpyxl` — install into a scratch venv, NOT the service env). Each contains a unique retrievable sentence ("The zebra migration report of 2026 ..."-style) plus, for the XLSX, enough rows (~500) to reproduce the giant-unsplit-chunk defect. Commit fixtures (each must be <100KB).
- [ ] **Step 2:** `test_compose_lifecycle.py` tests:
  - `test_upload_delete_gone_from_search_and_404`: upload txt with sentinel → searchable → `DELETE` → poll until search returns no sentinel hits AND `GET /v1/documents/{id}` → 404. Poll with the standard TIMEOUT; deletion lag is a finding if it exceeds it.
  - `test_pdf_docx_become_searchable` (`@pytest.mark.smoke` for PDF only): upload sample.pdf + sample.docx → their sentinels searchable.
  - `test_refresh_document_flow`: exercise `POST /v1/documents/{id}/refresh` on an uploaded doc; assert the documented response shape and terminal document status (read the router first for intended semantics; if refresh requires a source URI that uploads lack, assert the documented error shape instead — the point is pinning the contract live).
  - `test_xlsx_chunking_defect_pinned`: upload sample.xlsx → poll processed → fetch chunks via `GET /v1/chunks/{id}`; assert chunks are bounded (e.g. every chunk < 8000 chars). Mark `@pytest.mark.xfail(reason="known giant-chunk XLSX defect, see issue #<n>", strict=True)` — first check `gh issue list` for an existing issue for the tabular-chunking defect (architecture overview §6.2); create one if absent and reference it. `strict=True` so the fix PR must flip the mark.
- [ ] **Step 3:** `test_compose_event_durability.py` (`@pytest.mark.smoke`): REST search → returns `event_id` → immediately `POST /v1/feedback` (`answered`) → 2xx; then `GET /v1/scorecard` reflects a captured event (shape-level assert). This is the live pin for #240.
- [ ] **Step 4:** Run both files against the live stack → green (xfail counts as expected-fail). Lint/format; collection check; commit: `test: add lifecycle, binary-format, and event-durability live E2E (pins XLSX chunking defect)`.

### Task 7: Dead-letter recovery E2E (B4)

**Files:**
- Create: `services/inh-ingestion-svc/tests/failure_injection/test_compose_dead_letter_recovery.py`

**Interfaces:**
- Consumes: compose stack with docker CLI access from the test host; ingestion service's own REST surface (`/dead-letter`, dead-letter retry endpoint — read `services/inh-ingestion-svc/src/api/` routers for exact paths/ports first).

- [ ] **Step 1:** Read the dead-letter flow: `src/services/` dead-letter service, Temporal activity retry policy (how many attempts / how long before a job is dead-lettered — this determines test pacing), and the compose service name for Weaviate. Document findings as comments at the top of the test file.
- [ ] **Step 2:** Write the test (`compose` + `failure_injection` marks, NOT smoke; generous module timeout):
  - `test_dependency_outage_dead_letters_then_recovers`: verify `docker compose ps` shows the stack (skip otherwise). `docker compose stop weaviate` → upload a doc via public API → poll the ingestion API's dead-letter listing until a job for that document appears (bounded by the discovered retry policy + margin) → `docker compose start weaviate` → wait healthy → POST the retry endpoint → poll until document `processed` AND its sentinel is searchable via `/v1/search`.
  - Cleanup is unconditional (`finally`: `docker compose start weaviate`).
- [ ] **Step 3:** Run against the live stack → green. This is the slowest new test; record its wall-clock time in the task summary. It joins the full lane automatically (`compose` marker ⇒ `integration.yml`'s `compose and not eval_gate` selection). Verify that selection picks it up: `uv run pytest -m "compose and not eval_gate" --collect-only -q | grep dead_letter`.
- [ ] **Step 4:** Lint/format; collection check; commit: `test: add dead-letter outage-and-recovery live E2E`.

### Task 8: Eval-gate tolerance derived from corpus resolution

**Files:**
- Modify: `services/inh-public-api-svc/tests/evals/eval_gate.py`
- Modify: `services/inh-public-api-svc/tests/evals/test_eval_gate.py`
- Modify: `services/inh-public-api-svc/tests/evals/test_compose_retrieval_regression.py` (pass qrels-derived tolerance)
- Modify: `docs/adr/0003-traffic-mined-retrieval-evals.md` (amendment), `docs/testing.md`

**Interfaces:**
- Produces: `min_detectable_delta(metric: str, num_queries: int) -> float` and `effective_tolerance(metric: str, num_queries: int, floor: float = DEFAULT_TOLERANCE) -> float` in `eval_gate.py`; `find_regressions(..., tolerance=...)` unchanged in signature but callers pass per-metric effective tolerance. CLI gains `--num-queries` (or reads qrels path) to derive it.

- [ ] **Step 1:** Write failing unit tests in `test_eval_gate.py`:

```python
def test_min_detectable_delta_mrr():
    # smallest single-query MRR move: rank 1 -> 2 changes 1/1 - 1/2 = 0.5, averaged over n
    assert min_detectable_delta("mrr", 13) == pytest.approx(0.5 / 13)

def test_min_detectable_delta_recall():
    # one query gaining/losing one relevant doc: 1/n (conservative, single-relevant case)
    assert min_detectable_delta("recall_at_5", 13) == pytest.approx(1 / 13)

def test_min_detectable_delta_ndcg():
    # smallest top-2 swap: (1 - 1/log2(3)) / n
    assert min_detectable_delta("ndcg_at_5", 13) == pytest.approx((1 - 1 / math.log2(3)) / 13)

def test_effective_tolerance_takes_max_of_floor_and_resolution():
    assert effective_tolerance("mrr", 13, floor=0.02) == pytest.approx(0.5 / 13)   # resolution dominates
    assert effective_tolerance("mrr", 200, floor=0.02) == pytest.approx(0.02)      # floor dominates

def test_find_regressions_with_effective_tolerance_ignores_single_rank_slip():
    # baseline mrr .70, current .6615 (= one rank-1->2 slip at n=13): NOT a regression
    # baseline mrr .70, current .60: IS a regression
```

(Metric-name keys must match the baseline JSON's metric keys exactly — read `retrieval_baseline.json` first.)
- [ ] **Step 2:** Run → FAIL (functions missing).
- [ ] **Step 3:** Implement in `eval_gate.py`: the two functions + wire the gate path (`check` CLI + wherever `test_compose_retrieval_regression.py` invokes it) to compute `n` from the gated qrels pool (excluding `abstention` category, matching existing gating semantics) and use `effective_tolerance` per metric. `EVAL_GATE_TOLERANCE` env keeps its meaning as the *floor*.
- [ ] **Step 4:** Run the full evals unit-test file + the offline default suite → PASS.
- [ ] **Step 5:** Update `docs/testing.md` (tolerance section) and append an amendment block to ADR 0003 (dated, referencing #236/#237, stating tolerance = max(floor, per-metric minimum detectable single-query delta)). Update the `_comment` in `retrieval_baseline.json` to note tolerance is now derived (values untouched). Close-reference: add "Closes #236" to the eventual PR body, not the commit.
- [ ] **Step 6:** Commit: `fix(evals): derive eval-gate tolerance from corpus resolution (#236)`.

### Task 9: Rename mocked `tests/e2e/` → `tests/app_flows/`

**Files:**
- Rename: `services/inh-public-api-svc/tests/e2e/` → `services/inh-public-api-svc/tests/app_flows/`

- [ ] **Step 1:** `git mv services/inh-public-api-svc/tests/e2e services/inh-public-api-svc/tests/app_flows`. Update the module docstring in its `conftest.py` to state: "In-process app-flow tests with mocked dependencies. NOT end-to-end — live E2E lives in tests/integration/test_compose_*.py."
- [ ] **Step 2:** Grep for references: `grep -rn "tests/e2e" services/ docs/ Makefile .github/ AGENTS.md` — update every hit (docs/testing.md counts).
- [ ] **Step 3:** Run `uv run pytest tests/app_flows --no-cov -q` → all pass; full default suite collection unchanged in count.
- [ ] **Step 4:** Commit: `test: rename mocked tests/e2e to tests/app_flows (name stopped lying)`.

### Task 10: Ruleset repair + required checks (MAIN AGENT ONLY — admin action, pre-approved)

**Files:** none (GitHub API).

- [ ] **Step 1:** `gh api repos/inherent-prime/inherent/rulesets/16976743` — capture current JSON to scratchpad as backup.
- [ ] **Step 2:** PUT the corrected ruleset: `conditions.ref_name.include = ["refs/heads/main", "refs/heads/release*"]`; rules: keep `deletion`, `non_fast_forward`, `required_linear_history`; `pull_request` with `required_approving_review_count: 0`, `required_review_thread_resolution: true`; **drop `required_signatures`**; add `required_status_checks` with contexts `Required tests before merge`, `E2E smoke`, `Conventions` (verify exact check-run names against the PR's checks first: `gh pr checks <n>`); decide `copilot_code_review`/`code_quality` retention = keep as-is.
- [ ] **Step 3:** Verify: `gh api repos/inherent-prime/inherent/rules/branches/main` returns the full rule list (non-empty). Paste output into the final report.
- [ ] **Step 4:** Negative test after the PR is open: confirm the PR shows the three checks as Required in `gh pr checks`.

### Task 11: Docs — AGENTS.md branch policy + gate table

**Files:**
- Modify: `AGENTS.md`, `docs/testing.md`, `CHANGELOG.md`

- [ ] **Step 1:** AGENTS.md: replace the "PRs against dev" rule with: PRs target `main`; `dev` retired (kept only as scratch, no protection); add a gate table — required-on-PR (CI aggregate / E2E smoke / Conventions + skip labels) vs post-merge (integration + eval gate + benchmarks) vs release-only (publish approval, Hetzner E2E). Keep AGENTS.md's existing voice/format.
- [ ] **Step 2:** `docs/testing.md`: document the `smoke` marker, the smoke lane's scope and runtime budget, the tolerance derivation (cross-link ADR 0003 amendment), and the tests/app_flows rename.
- [ ] **Step 3:** `mkdocs build --strict` locally (docs CI parity). Finalize the CHANGELOG entry. Commit: `docs: main-only branch policy, merge-gate table, smoke lane + tolerance docs`.

### Task 12: Final verification + test report (MAIN AGENT judges)

- [ ] **Step 1:** Full local runs, fresh stack: `make quickstart`; then per service: default suite (`uv run pytest`), smoke selection (`-m "smoke and compose"`, timed), full compose selection (`-m "compose and not eval_gate"`), eval gate (`-m "eval_gate and compose"`). Record all counts/times/failures verbatim.
- [ ] **Step 2:** Push branch, open PR to `main` (body: summary, gate table, "Closes #236", test evidence). Watch all three required checks run on the PR itself; record E2E smoke wall-clock. If smoke exceeds 20 min, tune (drop binary-format case to full lane) before merge.
- [ ] **Step 3:** Produce the test report (docs/superpowers or PR body + artifact): before/after gate table, new-suite inventory, measured runtimes, defects found during the work (each with issue link), ruleset verification output.

## Self-Review (done at write time)

- Spec coverage: A1→T10, A2.1→T1, A2.2→T3, A2.3→T2, A3→T8+T7, A4→T11, B1→T4, B2→T5, B3→T6, B4→T7, rename→T9, report→T12. No gaps.
- Placeholders: none — all steps carry concrete code/commands or an explicit read-first instruction with the exact file to read.
- Interface consistency: `smoke` marker (T3) consumed by T4/T5/T6; check contexts (T1/T2/T3 job names) match T10's required contexts; principal-B naming consistent between T5 bootstrap and tests.
