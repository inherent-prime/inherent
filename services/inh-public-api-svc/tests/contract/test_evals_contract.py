"""Evals v1 REST contract regression tests (design spec: evals-v1, task 9).

Locks down the public REST contract for the evals surface:

- **Response shapes** of FeedbackResponse / ScorecardResponse / EvalCase list /
  EvalRunReport / the mutation endpoints' small JSON bodies.
- **Permissions** — feedback/scorecard/cases-list/run-report need 'search';
  case-patch/run-start/events-delete need 'write'.
- **Workspace resolution** — every evals endpoint requires a SINGLE resolved
  workspace; a multi-workspace (workspace_id=None) caller gets 400.
- **Error shape** — unknown event_id → 404 with retention-window guidance;
  zero active cases → 409 with quickstart guidance.

All offline: auth dependencies are overridden per-test to select a permission
set, the DB / search service are mocked, and the lifespan DB init is stubbed.
Mirrors tests/contract/test_rest_contract.py's structure and reuses
tests/contract/conftest.py's fixtures (search_only_key, write_key, full_key,
single_workspace_db, mock_search_service, etc).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.models.evals import EvalRunReport
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_read_permission,
    get_search_permission,
    get_write_permission,
    resolve_workspace_read,
    resolve_workspace_search,
    resolve_workspace_write,
)
from src.services.database import get_database
from src.services.search import get_search_service

pytestmark = [pytest.mark.contract]


# --------------------------------------------------------------------------- #
# App / client helpers — mirrors test_rest_contract.py exactly.
# --------------------------------------------------------------------------- #
_UNSET = object()


def _resolved(key: APIKeyInfo, workspace_id) -> ResolvedAuth:
    resolved_ws = key.workspace_id if workspace_id is _UNSET else workspace_id
    return ResolvedAuth(key_info=key, workspace_id=resolved_ws)


def _build_app(
    *,
    key: APIKeyInfo,
    db: AsyncMock | None = None,
    search_svc: AsyncMock | None = None,
    override_permissions: bool = True,
    workspace_id=_UNSET,
):
    """Create an app with auth dependencies overridden.

    When ``override_permissions`` is True the per-route permission gates are
    overridden so the route body runs. When False, only ``get_api_key_info``
    is overridden so the REAL permission gates run (403 tests).
    """
    app = create_app()
    app.dependency_overrides[get_api_key_info] = lambda: key

    if db is not None:
        app.dependency_overrides[get_database] = lambda: db
    if search_svc is not None:
        app.dependency_overrides[get_search_service] = lambda: search_svc

    if override_permissions:
        resolved = _resolved(key, workspace_id)
        app.dependency_overrides[get_read_permission] = lambda: key
        app.dependency_overrides[get_search_permission] = lambda: key
        app.dependency_overrides[get_write_permission] = lambda: key
        app.dependency_overrides[resolve_workspace_read] = lambda: resolved
        app.dependency_overrides[resolve_workspace_search] = lambda: resolved
        app.dependency_overrides[resolve_workspace_write] = lambda: resolved
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_HDR = {"X-API-Key": "ink_test"}

_NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _sample_run_summary(run_id: str = "run_1", status: str = "completed") -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "case_count": 3,
        "k": 5,
        "aggregates": {
            "keyword": {"recall_at_k": 0.5, "mrr": 0.5, "ndcg_at_k": 0.5},
            "semantic": {"recall_at_k": 0.8, "mrr": 0.7, "ndcg_at_k": 0.75},
            "hybrid": {"recall_at_k": 0.9, "mrr": 0.85, "ndcg_at_k": 0.88},
        },
        "created_at": _NOW,
        "finished_at": _NOW,
        "error": None,
    }


@pytest.fixture
def evals_db() -> AsyncMock:
    """Mock DB stubbed for the evals surface, single-workspace ('ws-1')."""
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])

    # Feedback path
    db.get_eval_event = AsyncMock(
        return_value={
            "event_id": "evt_1",
            "workspace_id": "ws-1",
            "query_text": "revenue this quarter",
            "search_mode": "hybrid",
            "result_doc_ids": ["doc-1"],
            "result_chunk_ids": ["chunk-1"],
        }
    )
    db.upsert_eval_feedback = AsyncMock(return_value=None)
    db.upsert_eval_case = AsyncMock(return_value="case_1")

    # Scorecard path
    db.eval_scorecard_counts = AsyncMock(
        return_value={
            "captured_events": 10,
            "verdict_distribution": {},
            "feedback_distribution": {"answered": 2, "partial": 1},
            "corpus_gaps": [],
            "eval_case_count": 2,
        }
    )
    db.get_last_eval_run = AsyncMock(return_value=None)

    # Cases path
    db.list_eval_cases = AsyncMock(
        return_value=[
            {
                "case_id": "case_1",
                "workspace_id": "ws-1",
                "query_text": "revenue this quarter",
                "expected_doc_ids": ["doc-1"],
                "relevance_grade": 2,
                "active": True,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
        ]
    )
    db.set_eval_case_active = AsyncMock(return_value=True)

    # Runs path
    db.get_active_eval_cases = AsyncMock(return_value=[])
    db.get_eval_case_ids = AsyncMock(return_value=set())
    db.insert_eval_run = AsyncMock(return_value=None)
    db.get_eval_run = AsyncMock(return_value=None)
    db.get_eval_run_results = AsyncMock(return_value=[])

    # Events purge
    db.delete_eval_events = AsyncMock(return_value={"events_deleted": 5, "cases_deleted": 0})

    return db


# =========================================================================== #
# 1. POST feedback — known event, search-permission key -> 200
# =========================================================================== #
class TestFeedbackShape:
    async def test_feedback_known_event_returns_200(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/feedback",
                json={"event_id": "evt_1", "verdict": "answered"},
                headers=_HDR,
            )
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"event_id", "promoted", "case_id"}
        assert body["event_id"] == "evt_1"
        assert body["promoted"] is True
        assert body["case_id"] == "case_1"

    # ----------------------------------------------------------------- #
    # 2. POST feedback — unknown event -> 404 RFC7807, mentions retention
    # ----------------------------------------------------------------- #
    async def test_feedback_unknown_event_returns_404_with_retention_guidance(
        self, search_only_key, evals_db
    ):
        evals_db.get_eval_event = AsyncMock(return_value=None)
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/feedback",
                json={"event_id": "evt_missing", "verdict": "answered"},
                headers=_HDR,
            )
        assert r.status_code == 404
        body = r.json()
        assert "detail" in body
        assert "retention" in body["detail"].lower()

    # ----------------------------------------------------------------- #
    # 3. POST feedback — read-only key -> 403 (needs search)
    # ----------------------------------------------------------------- #
    async def test_feedback_requires_search_permission(self, read_key, evals_db):
        app = _build_app(key=read_key, db=evals_db, override_permissions=False)
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/feedback",
                json={"event_id": "evt_1", "verdict": "answered"},
                headers=_HDR,
            )
        assert r.status_code == 403


# =========================================================================== #
# 4. GET scorecard -> 200 with summary str + low_confidence bool
# =========================================================================== #
class TestScorecardShape:
    async def test_scorecard_shape(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.get("/v1/evals/scorecard", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["summary"], str)
        assert isinstance(body["low_confidence"], bool)
        assert body["workspace_id"] == "ws-1"


# =========================================================================== #
# 5. GET cases -> 200 list shape
# =========================================================================== #
class TestCasesListShape:
    async def test_list_cases_shape(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.get("/v1/evals/cases", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert body[0]["case_id"] == "case_1"
        for field in (
            "case_id",
            "workspace_id",
            "query_text",
            "expected_doc_ids",
            "relevance_grade",
            "active",
            "created_at",
            "updated_at",
        ):
            assert field in body[0], field


# =========================================================================== #
# 6/7. PATCH case — permission + success
# =========================================================================== #
class TestPatchCase:
    async def test_patch_case_requires_write_permission(self, search_only_key, evals_db):
        """A search-only key (no 'write') cannot patch a case -> 403."""
        app = _build_app(key=search_only_key, db=evals_db, override_permissions=False)
        async with _client(app) as c:
            r = await c.patch("/v1/evals/cases/case_1", json={"active": False}, headers=_HDR)
        assert r.status_code == 403

    async def test_patch_case_with_write_key_returns_200(self, write_key, evals_db):
        app = _build_app(key=write_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.patch("/v1/evals/cases/case_1", json={"active": False}, headers=_HDR)
        assert r.status_code == 200

    async def test_patch_case_not_found_returns_404(self, write_key, evals_db):
        evals_db.set_eval_case_active = AsyncMock(return_value=False)
        app = _build_app(key=write_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.patch("/v1/evals/cases/case_missing", json={"active": False}, headers=_HDR)
        assert r.status_code == 404


# =========================================================================== #
# 8/9. POST runs — 409 no active cases / 202 accepted
# =========================================================================== #
class TestStartRun:
    async def test_start_run_zero_active_cases_returns_409_with_guidance(
        self, write_key, evals_db, mock_search_service
    ):
        evals_db.get_active_eval_cases = AsyncMock(return_value=[])
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", headers=_HDR)
        assert r.status_code == 409
        body = r.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "quickstart" in detail_lower or "local.md" in detail_lower

    async def test_start_run_with_cases_returns_202(self, write_key, evals_db, mock_search_service):
        evals_db.get_active_eval_cases = AsyncMock(
            return_value=[
                {
                    "case_id": "case_1",
                    "query_text": "revenue this quarter",
                    "expected_doc_ids": ["doc-1"],
                    "relevance_grade": 2,
                }
            ]
        )
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", headers=_HDR)
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "running"
        assert "run_id" in body

    # ------------------------------------------------------------------- #
    # Run scoping (#250): optional case_ids / since narrow the replay set.
    # Omitting both keeps the default (every active case) -- covered above.
    # ------------------------------------------------------------------- #
    async def test_start_run_unscoped_replays_every_active_case(
        self, write_key, evals_db, mock_search_service
    ):
        """Regression guard: no body at all -> both DB fetches stay unscoped."""
        evals_db.get_active_eval_cases = AsyncMock(
            return_value=[
                {
                    "case_id": "case_1",
                    "query_text": "revenue this quarter",
                    "expected_doc_ids": ["doc-1"],
                    "relevance_grade": 2,
                }
            ]
        )
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", headers=_HDR)
        assert r.status_code == 202
        # BackgroundTasks run inline under ASGITransport, so both the initial
        # (start_run) and executing (execute_run) fetches have happened by now.
        assert evals_db.get_active_eval_cases.await_count == 2
        for call in evals_db.get_active_eval_cases.call_args_list:
            assert call.kwargs == {"workspace_id": "ws-1", "case_ids": None, "since": None}
        evals_db.get_eval_case_ids.assert_not_awaited()

    async def test_start_run_with_case_ids_scopes_both_db_fetches(
        self, write_key, evals_db, mock_search_service
    ):
        """The same case_ids scope must reach BOTH start_run's and
        execute_run's independent get_active_eval_cases call (#250's core bug:
        scoping only one of the two call sites)."""
        evals_db.get_eval_case_ids = AsyncMock(return_value={"case_1"})
        evals_db.get_active_eval_cases = AsyncMock(
            return_value=[
                {
                    "case_id": "case_1",
                    "query_text": "revenue this quarter",
                    "expected_doc_ids": ["doc-1"],
                    "relevance_grade": 2,
                }
            ]
        )
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", json={"case_ids": ["case_1"]}, headers=_HDR)
        assert r.status_code == 202
        evals_db.get_eval_case_ids.assert_awaited_once_with(
            workspace_id="ws-1", case_ids=["case_1"]
        )
        assert evals_db.get_active_eval_cases.await_count == 2
        for call in evals_db.get_active_eval_cases.call_args_list:
            assert call.kwargs == {
                "workspace_id": "ws-1",
                "case_ids": ["case_1"],
                "since": None,
            }

    async def test_start_run_with_since_scopes_both_db_fetches(
        self, write_key, evals_db, mock_search_service
    ):
        evals_db.get_active_eval_cases = AsyncMock(
            return_value=[
                {
                    "case_id": "case_1",
                    "query_text": "revenue this quarter",
                    "expected_doc_ids": ["doc-1"],
                    "relevance_grade": 2,
                }
            ]
        )
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/runs",
                json={"since": "2026-08-01T00:00:00Z"},
                headers=_HDR,
            )
        assert r.status_code == 202
        for call in evals_db.get_active_eval_cases.call_args_list:
            assert call.kwargs["case_ids"] is None
            assert call.kwargs["since"] is not None

    async def test_start_run_with_foreign_case_id_returns_404(
        self, write_key, evals_db, mock_search_service
    ):
        """A case_id the caller's workspace does not own must be rejected
        outright, never silently dropped from the replay set (#250) -- matches
        this codebase's existing cross-tenant-id convention (404, no leak)."""
        evals_db.get_eval_case_ids = AsyncMock(return_value=set())  # nothing found in ws-1
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", json={"case_ids": ["case_other_ws"]}, headers=_HDR)
        assert r.status_code == 404
        assert "case_other_ws" in r.json()["detail"]
        # Must reject before ever touching the replay set.
        evals_db.get_active_eval_cases.assert_not_awaited()
        evals_db.insert_eval_run.assert_not_awaited()

    async def test_start_run_with_partial_foreign_case_ids_returns_404(
        self, write_key, evals_db, mock_search_service
    ):
        """Mixed request (one real id, one foreign/unknown id) rejects the
        whole request rather than silently widening or silently narrowing."""
        evals_db.get_eval_case_ids = AsyncMock(return_value={"case_1"})  # case_2 missing
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/runs", json={"case_ids": ["case_1", "case_2"]}, headers=_HDR
            )
        assert r.status_code == 404
        assert "case_2" in r.json()["detail"]
        evals_db.get_active_eval_cases.assert_not_awaited()


# =========================================================================== #
# 10. GET run — unknown -> 404
# =========================================================================== #
class TestGetRun:
    async def test_get_run_unknown_returns_404(self, search_only_key, evals_db):
        evals_db.get_eval_run = AsyncMock(return_value=None)
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.get("/v1/evals/runs/run_missing", headers=_HDR)
        assert r.status_code == 404

    async def test_get_run_found_returns_200(self, search_only_key, evals_db):
        evals_db.get_eval_run = AsyncMock(return_value=_sample_run_summary())
        evals_db.get_eval_run_results = AsyncMock(
            return_value=[
                {
                    "case_id": "case_1",
                    "query_text": "revenue this quarter",
                    "mode": "hybrid",
                    "recall_at_k": 0.9,
                    "mrr": 0.85,
                    "ndcg_at_k": 0.88,
                }
            ]
        )
        app = _build_app(key=search_only_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.get("/v1/evals/runs/run_1", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        EvalRunReport.model_validate(body)
        assert body["run"]["run_id"] == "run_1"
        assert body["per_case"][0]["case_id"] == "case_1"


# =========================================================================== #
# 11. DELETE events — write key -> 200 {"deleted": n}
# =========================================================================== #
class TestDeleteEvents:
    async def test_delete_events_with_write_key_returns_200(self, write_key, evals_db):
        app = _build_app(key=write_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.delete("/v1/evals/events", headers=_HDR)
        assert r.status_code == 200
        assert r.json() == {"deleted": 5, "cases_deleted": 0}
        evals_db.delete_eval_events.assert_awaited_once_with(
            workspace_id="ws-1", include_cases=False
        )

    async def test_delete_events_requires_write_permission(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, override_permissions=False)
        async with _client(app) as c:
            r = await c.delete("/v1/evals/events", headers=_HDR)
        assert r.status_code == 403

    # ------------------------------------------------------------------- #
    # Case purge (#250): opt-in via ?include_cases=true. Default False keeps
    # the documented contract (raw events ephemeral, labeled cases durable)
    # that tests/evals/test_evals_flywheel.py:183-193 asserts against a live
    # stack -- this is the offline regression guard for the same contract.
    # ------------------------------------------------------------------- #
    async def test_delete_events_default_leaves_cases_untouched(self, write_key, evals_db):
        app = _build_app(key=write_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.delete("/v1/evals/events", headers=_HDR)
        assert r.status_code == 200
        assert evals_db.delete_eval_events.call_args.kwargs["include_cases"] is False

    async def test_delete_events_include_cases_true_purges_cases(self, write_key, evals_db):
        evals_db.delete_eval_events = AsyncMock(
            return_value={"events_deleted": 5, "cases_deleted": 3}
        )
        app = _build_app(key=write_key, db=evals_db, workspace_id="ws-1")
        async with _client(app) as c:
            r = await c.delete("/v1/evals/events?include_cases=true", headers=_HDR)
        assert r.status_code == 200
        assert r.json() == {"deleted": 5, "cases_deleted": 3}
        evals_db.delete_eval_events.assert_awaited_once_with(
            workspace_id="ws-1", include_cases=True
        )


# =========================================================================== #
# 12. Multi-workspace auth (workspace_id=None) -> 400 on every evals endpoint
# =========================================================================== #
class TestMultiWorkspaceRequires400:
    async def test_feedback_multi_workspace_returns_400(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.post(
                "/v1/evals/feedback",
                json={"event_id": "evt_1", "verdict": "answered"},
                headers=_HDR,
            )
        assert r.status_code == 400

    async def test_scorecard_multi_workspace_returns_400(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.get("/v1/evals/scorecard", headers=_HDR)
        assert r.status_code == 400

    async def test_list_cases_multi_workspace_returns_400(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.get("/v1/evals/cases", headers=_HDR)
        assert r.status_code == 400

    async def test_patch_case_multi_workspace_returns_400(self, write_key, evals_db):
        app = _build_app(key=write_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.patch("/v1/evals/cases/case_1", json={"active": False}, headers=_HDR)
        assert r.status_code == 400

    async def test_start_run_multi_workspace_returns_400(
        self, write_key, evals_db, mock_search_service
    ):
        app = _build_app(
            key=write_key, db=evals_db, search_svc=mock_search_service, workspace_id=None
        )
        async with _client(app) as c:
            r = await c.post("/v1/evals/runs", headers=_HDR)
        assert r.status_code == 400

    async def test_get_run_multi_workspace_returns_400(self, search_only_key, evals_db):
        app = _build_app(key=search_only_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.get("/v1/evals/runs/run_1", headers=_HDR)
        assert r.status_code == 400

    async def test_delete_events_multi_workspace_returns_400(self, write_key, evals_db):
        app = _build_app(key=write_key, db=evals_db, workspace_id=None)
        async with _client(app) as c:
            r = await c.delete("/v1/evals/events", headers=_HDR)
        assert r.status_code == 400
