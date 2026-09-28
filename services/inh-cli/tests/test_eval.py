"""Tests for `inherent eval run` (inherent#391)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

import inh_cli.client as client_mod
from inh_cli.commands.eval import DatasetError, load_dataset
from inh_cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_dataset(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "dataset.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# ---------------------------------------------------------------------------
# load_dataset: parsing / validation
# ---------------------------------------------------------------------------


def test_load_dataset_missing_file_raises(tmp_path):
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "nope.yaml")


def test_load_dataset_invalid_yaml_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("not: valid: yaml: [")
    with pytest.raises(DatasetError, match="Invalid YAML"):
        load_dataset(path)


def test_load_dataset_non_mapping_top_level_raises(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(DatasetError, match="top level must be a mapping"):
        load_dataset(path)


def test_load_dataset_missing_queries_raises(tmp_path):
    path = _write_dataset(tmp_path, {"name": "x"})
    with pytest.raises(DatasetError, match="'queries'"):
        load_dataset(path)


def test_load_dataset_query_missing_query_text_raises(tmp_path):
    path = _write_dataset(tmp_path, {"queries": [{"id": "q1", "expected_ids": ["d1"]}]})
    with pytest.raises(DatasetError, match="'query'"):
        load_dataset(path)


def test_load_dataset_query_missing_expected_ids_raises(tmp_path):
    path = _write_dataset(tmp_path, {"queries": [{"id": "q1", "query": "hello"}]})
    with pytest.raises(DatasetError, match="'expected_ids'"):
        load_dataset(path)


def test_load_dataset_parses_full_shape(tmp_path):
    path = _write_dataset(
        tmp_path,
        {
            "name": "support-eval",
            "k": 3,
            "queries": [
                {
                    "id": "q1",
                    "query": "reset password",
                    "expected_ids": ["doc_1", "chunk_2"],
                    "filters": {"category": "billing"},
                }
            ],
        },
    )
    data = load_dataset(path)
    assert data["name"] == "support-eval"
    assert data["k"] == 3
    assert data["queries"] == [
        {
            "id": "q1",
            "query": "reset password",
            "expected_ids": ["doc_1", "chunk_2"],
            "filters": {"category": "billing"},
        }
    ]


def test_load_dataset_defaults_name_to_filename_stem_and_id_to_position(tmp_path):
    path = tmp_path / "my_eval.yaml"
    path.write_text(yaml.safe_dump({"queries": [{"query": "q", "expected_ids": ["d1"]}]}))
    data = load_dataset(path)
    assert data["name"] == "my_eval"
    assert data["queries"][0]["id"] == "1"


# ---------------------------------------------------------------------------
# eval run: end to end against a mocked search API
# ---------------------------------------------------------------------------


def _search_response(chunk_id: str, document_id: str) -> dict:
    return {"results": [{"chunk_id": chunk_id, "document_id": document_id, "score": 0.9}]}


def test_eval_run_reports_hit_and_mrr_for_a_perfect_match(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "find doc 1", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        body = json.loads(request.content)
        assert body == {"query": "find doc 1", "limit": 5, "search_mode": "hybrid"}
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset)])

    assert result.exit_code == 0, result.output
    assert "hit@5=1.00" in result.stdout
    assert "mrr=1.00" in result.stdout


def test_eval_run_reports_miss_when_nothing_expected_is_returned(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "find doc 1", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response("c9", "doc_9"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset)])

    assert result.exit_code == 0, result.output
    assert "hit@5=0.00" in result.stdout
    assert "mrr=0.00" in result.stdout


def test_eval_run_matches_on_chunk_id_too(api_env, inherent_home, runner, tmp_path, monkeypatch):
    """expected_ids matches EITHER document_id or chunk_id (inherent#391)."""
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "q", "expected_ids": ["chunk_xyz"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response("chunk_xyz", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset)])

    assert result.exit_code == 0, result.output
    assert "hit@5=1.00" in result.stdout


def test_eval_run_respects_k_flag_override(api_env, inherent_home, runner, tmp_path, monkeypatch):
    dataset = _write_dataset(
        tmp_path,
        {"k": 10, "queries": [{"id": "q1", "query": "q", "expected_ids": ["doc_1"]}]},
    )

    seen_limits = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_limits.append(body["limit"])
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset), "--k", "2"])

    assert result.exit_code == 0, result.output
    assert seen_limits == [2]  # --k wins over the dataset's own k=10


def test_eval_run_filters_pass_through_to_search_request(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    """filters (#390's SearchRequest.filters) pass through unchanged."""
    dataset = _write_dataset(
        tmp_path,
        {
            "queries": [
                {
                    "id": "q1",
                    "query": "q",
                    "expected_ids": ["doc_1"],
                    "filters": {"category": "billing"},
                }
            ]
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["filters"] == {"category": "billing"}
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset)])
    assert result.exit_code == 0, result.output


def test_eval_run_exits_nonzero_below_min_hit_rate(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "q", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response("c9", "doc_9"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset), "--min-hit-rate", "0.5"])

    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_eval_run_meeting_min_hit_rate_exits_zero(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "q", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset), "--min-hit-rate", "0.5"])

    assert result.exit_code == 0, result.output


def test_eval_run_json_output_is_structured(api_env, inherent_home, runner, tmp_path, monkeypatch):
    dataset = _write_dataset(
        tmp_path,
        {"name": "my-eval", "queries": [{"id": "q1", "query": "q", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["--json", "eval", "run", "--dataset", str(dataset)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dataset"] == "my-eval"
    assert payload["hit_rate"] == 1.0
    assert payload["mrr"] == 1.0
    assert payload["query_count"] == 1
    assert payload["queries"][0]["id"] == "q1"


def test_eval_run_multiple_queries_average_correctly(
    api_env, inherent_home, runner, tmp_path, monkeypatch
):
    dataset = _write_dataset(
        tmp_path,
        {
            "queries": [
                {"id": "q1", "query": "hit", "expected_ids": ["doc_1"]},
                {"id": "q2", "query": "miss", "expected_ids": ["doc_2"]},
            ]
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["query"] == "hit":
            return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)
        return httpx.Response(200, json=_search_response("c9", "doc_9"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["--json", "eval", "run", "--dataset", str(dataset)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["hit_rate"] == 0.5


def test_eval_run_workspace_flag_sets_header(api_env, inherent_home, runner, tmp_path, monkeypatch):
    dataset = _write_dataset(
        tmp_path,
        {"queries": [{"id": "q1", "query": "q", "expected_ids": ["doc_1"]}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Workspace-Id"] == "ws_abc"
        return httpx.Response(200, json=_search_response("c1", "doc_1"), request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["eval", "run", "--dataset", str(dataset), "--workspace", "ws_abc"])
    assert result.exit_code == 0, result.output
