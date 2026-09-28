"""Tests for the purge_workspace activities (inherent#395).

Each activity is expected to be idempotent -- running it twice against the
same workspace must not error and must not double-count/double-delete.
These tests mock the shared services (same convention as
test_temporal_activities.py) and assert that behavior directly, plus that
`verify_purge` genuinely reports residue rather than always-zero.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.temporal.activities.purge import (
    cancel_inflight_ingestion,
    mark_workspace_purged,
    mark_workspace_purging,
    purge_audit_logs,
    purge_postgres_documents,
    purge_postgres_side_tables,
    purge_weaviate_collection,
    record_purge_receipt,
    revoke_workspace_api_keys,
    verify_purge,
)
from src.temporal.models import PurgeWorkspaceInput


def _input(**overrides) -> PurgeWorkspaceInput:
    defaults = {"workspace_id": "ws_purge_1", "operator": "operator@example.com"}
    defaults.update(overrides)
    return PurgeWorkspaceInput(**defaults)


# =========================================================================
# mark_workspace_purging / mark_workspace_purged
# =========================================================================


class TestMarkWorkspacePurgeState:
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_mark_purging_sets_status(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.set_workspace_purge_status = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        await mark_workspace_purging(_input())

        mock_db.set_workspace_purge_status.assert_awaited_once_with("ws_purge_1", "purging")

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_mark_purging_is_idempotent(self, mock_get_db):
        """Running the same mark twice must not error -- upsert semantics."""
        mock_db = MagicMock()
        mock_db.set_workspace_purge_status = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        await mark_workspace_purging(_input())
        await mark_workspace_purging(_input())

        assert mock_db.set_workspace_purge_status.await_count == 2

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_mark_purged_sets_terminal_status(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.set_workspace_purge_status = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        await mark_workspace_purged(_input())

        mock_db.set_workspace_purge_status.assert_awaited_once_with("ws_purge_1", "purged")


# =========================================================================
# cancel_inflight_ingestion
# =========================================================================


class TestCancelInflightIngestion:
    @patch("src.temporal.shared_services.get_temporal_client")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_cancels_every_in_flight_document(self, mock_get_db, mock_get_client):
        mock_db = MagicMock()
        mock_db.get_in_flight_document_ids = AsyncMock(return_value=["doc1", "doc2"])
        mock_get_db.return_value = mock_db

        mock_handle = MagicMock()
        mock_handle.cancel = AsyncMock(return_value=None)
        mock_client = MagicMock()
        mock_client.get_workflow_handle.return_value = mock_handle
        mock_get_client.return_value = mock_client

        cancelled = await cancel_inflight_ingestion(_input())

        assert cancelled == 2
        mock_client.get_workflow_handle.assert_any_call("ingest-doc1")
        mock_client.get_workflow_handle.assert_any_call("ingest-doc2")

    @patch("src.temporal.shared_services.get_temporal_client")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_no_in_flight_documents_cancels_nothing(self, mock_get_db, mock_get_client):
        """Second run of the purge (nothing left in-flight) must be a clean idempotent no-op."""
        mock_db = MagicMock()
        mock_db.get_in_flight_document_ids = AsyncMock(return_value=[])
        mock_get_db.return_value = mock_db
        mock_get_client.return_value = MagicMock()

        cancelled = await cancel_inflight_ingestion(_input())

        assert cancelled == 0

    @patch("src.temporal.shared_services.get_temporal_client")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_already_closed_workflow_is_not_an_error(self, mock_get_db, mock_get_client):
        """A workflow that already finished (or was already cancelled) must not fail the activity."""
        from temporalio.service import RPCError

        mock_db = MagicMock()
        mock_db.get_in_flight_document_ids = AsyncMock(return_value=["doc1"])
        mock_get_db.return_value = mock_db

        mock_handle = MagicMock()
        mock_handle.cancel = AsyncMock(side_effect=RPCError("not found", None, None))
        mock_client = MagicMock()
        mock_client.get_workflow_handle.return_value = mock_handle
        mock_get_client.return_value = mock_client

        cancelled = await cancel_inflight_ingestion(_input())

        assert cancelled == 0


# =========================================================================
# Delete activities -- idempotent (deleting twice deletes 0 the 2nd time)
# =========================================================================


class TestDeleteActivitiesIdempotent:
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_purge_postgres_documents_second_run_deletes_nothing(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.delete_workspace_data = AsyncMock(side_effect=[3, 0])
        mock_get_db.return_value = mock_db

        first = await purge_postgres_documents(_input())
        second = await purge_postgres_documents(_input())

        assert (first, second) == (3, 0)

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_purge_postgres_side_tables_merges_and_is_idempotent(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.delete_workspace_side_tables = AsyncMock(
            side_effect=[{"dead_letter_jobs": 2}, {"dead_letter_jobs": 0}]
        )
        mock_db.delete_workspace_eval_data = AsyncMock(
            side_effect=[{"eval_cases": 1}, {"eval_cases": 0}]
        )
        mock_get_db.return_value = mock_db

        first = await purge_postgres_side_tables(_input())
        second = await purge_postgres_side_tables(_input())

        assert first == {"dead_letter_jobs": 2, "eval_cases": 1}
        assert second == {"dead_letter_jobs": 0, "eval_cases": 0}

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_revoke_api_keys_second_run_revokes_nothing(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.revoke_workspace_api_keys = AsyncMock(side_effect=[2, 0])
        mock_get_db.return_value = mock_db

        first = await revoke_workspace_api_keys(_input())
        second = await revoke_workspace_api_keys(_input())

        assert (first, second) == (2, 0)

    @patch("src.temporal.shared_services.get_weaviate_service")
    @pytest.mark.asyncio
    async def test_purge_weaviate_collection_second_run_is_noop(self, mock_get_weaviate):
        mock_weaviate = MagicMock()
        # delete_workspace_collection already checks existence internally
        # (see weaviate.py) -- True then False models that idempotent shape.
        mock_weaviate.delete_workspace_collection = AsyncMock(side_effect=[True, False])
        mock_get_weaviate.return_value = mock_weaviate

        first = await purge_weaviate_collection(_input())
        second = await purge_weaviate_collection(_input())

        assert (first, second) == (True, False)

    @pytest.mark.asyncio
    async def test_purge_weaviate_collection_no_weaviate_service_is_safe(self):
        with patch("src.temporal.shared_services.get_weaviate_service", return_value=None):
            result = await purge_weaviate_collection(_input())
        assert result is False

    @patch("src.config.settings.get_settings", return_value=MagicMock())
    @patch("src.services.audit_mongo_writer.delete_workspace_audit_logs")
    @pytest.mark.asyncio
    async def test_purge_audit_logs_default_purges(self, mock_delete, _mock_settings):
        mock_delete.return_value = 5

        deleted = await purge_audit_logs(_input(retain_audit_logs=False))

        assert deleted == 5
        mock_delete.assert_awaited_once()

    @patch("src.services.audit_mongo_writer.delete_workspace_audit_logs")
    @pytest.mark.asyncio
    async def test_purge_audit_logs_retained_skips_delete(self, mock_delete):
        """Operator opt-in: retain_audit_logs=True must never touch Mongo."""
        deleted = await purge_audit_logs(_input(retain_audit_logs=True))

        assert deleted == 0
        mock_delete.assert_not_called()


# =========================================================================
# verify_purge -- must detect real residue, not just report zeros
# =========================================================================


class TestVerifyPurgeDetectsResidue:
    @patch("src.config.settings.get_settings", return_value=MagicMock())
    @patch("src.services.audit_mongo_writer.count_workspace_audit_logs")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_reports_nonzero_residue(
        self, mock_get_db, mock_get_weaviate, mock_count_audit, _mock_settings
    ):
        mock_db = MagicMock()
        mock_db.count_workspace_residue = AsyncMock(
            return_value={"processed_documents": 3, "document_chunks": 0}
        )
        mock_get_db.return_value = mock_db

        mock_weaviate = MagicMock()
        mock_weaviate.workspace_collection_object_count.return_value = 7
        mock_get_weaviate.return_value = mock_weaviate

        mock_count_audit.return_value = 2

        report = await verify_purge(_input(retain_audit_logs=False))

        assert report["processed_documents"] == 3
        assert report["weaviate_collection_objects"] == 7
        assert report["audit_logs"] == 2
        # Not all zero -- a caller computing `verified` from this must see False.
        assert not all(v <= 0 for v in report.values())

    @patch("src.config.settings.get_settings", return_value=MagicMock())
    @patch("src.services.audit_mongo_writer.count_workspace_audit_logs")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_reports_all_zero_after_a_real_purge(
        self, mock_get_db, mock_get_weaviate, mock_count_audit, _mock_settings
    ):
        mock_db = MagicMock()
        mock_db.count_workspace_residue = AsyncMock(
            return_value={"processed_documents": 0, "document_chunks": 0}
        )
        mock_get_db.return_value = mock_db

        mock_weaviate = MagicMock()
        mock_weaviate.workspace_collection_object_count.return_value = 0
        mock_get_weaviate.return_value = mock_weaviate

        mock_count_audit.return_value = 0

        report = await verify_purge(_input(retain_audit_logs=False))

        assert all(v <= 0 for v in report.values())

    @patch("src.services.audit_mongo_writer.count_workspace_audit_logs")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_retained_audit_logs_excluded_from_report(
        self, mock_get_db, mock_get_weaviate, mock_count_audit
    ):
        """When audit logs are retained on purpose, they must not appear as 'residue'."""
        mock_db = MagicMock()
        mock_db.count_workspace_residue = AsyncMock(return_value={})
        mock_get_db.return_value = mock_db
        mock_get_weaviate.return_value = MagicMock(
            workspace_collection_object_count=MagicMock(return_value=0)
        )

        report = await verify_purge(_input(retain_audit_logs=True))

        assert "audit_logs" not in report
        mock_count_audit.assert_not_called()


# =========================================================================
# record_purge_receipt -- content-free, idempotent upsert
# =========================================================================


class TestRecordPurgeReceipt:
    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_receipt_has_no_document_content(self, mock_get_db):
        """The receipt written must be counts/flags/identifiers only -- never text."""
        mock_db = MagicMock()
        mock_db.record_purge_receipt = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        counts_before = {"processed_documents": 3}
        counts_after = {"processed_documents": 0}

        await record_purge_receipt(_input(), "purge-ws_purge_1", counts_before, counts_after)

        mock_db.record_purge_receipt.assert_awaited_once()
        _, kwargs = mock_db.record_purge_receipt.call_args
        recorded_values = {
            k: v
            for k, v in kwargs.items()
            if k not in ("purge_workflow_id", "workspace_id", "operator")
        }
        for value in recorded_values.values():
            # Every remaining field is an int, dict-of-ints, or bool -- no strings
            # of free text that could ever hold document content.
            assert isinstance(value, (bool, dict)) or isinstance(value, int)
            if isinstance(value, dict):
                assert all(isinstance(v, int) for v in value.values())

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_verified_true_only_when_all_counts_zero(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.record_purge_receipt = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        await record_purge_receipt(_input(), "purge-ws_purge_1", {"a": 1}, {"a": 0, "b": 0})

        _, kwargs = mock_db.record_purge_receipt.call_args
        assert kwargs["verified"] is True

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_verified_false_when_residue_remains(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.record_purge_receipt = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        await record_purge_receipt(_input(), "purge-ws_purge_1", {"a": 1}, {"a": 1})

        _, kwargs = mock_db.record_purge_receipt.call_args
        assert kwargs["verified"] is False
