"""Integration tests for the workspace-purge DatabaseService methods (inherent#395).

Runs against the local Postgres the repo's `db_service` fixture already
uses (see tests/conftest.py) -- these are NOT mocked, they exercise the
real SQL each purge activity runs.
"""

from __future__ import annotations

import pytest

from src.services.database import DatabaseService


def _ws(suffix: str) -> str:
    """Every workspace id here starts with 'test_' so conftest's cleanup fixture reaches it."""
    return f"test_purge_ws_{suffix}"


class TestWorkspacePurgeStatus:
    @pytest.mark.asyncio
    async def test_unset_workspace_has_no_purge_status(self, db_service: DatabaseService):
        assert await db_service.get_workspace_purge_status(_ws("unset")) is None

    @pytest.mark.asyncio
    async def test_set_then_get_purge_status(self, db_service: DatabaseService):
        workspace_id = _ws("set")

        await db_service.set_workspace_purge_status(workspace_id, "purging")
        assert await db_service.get_workspace_purge_status(workspace_id) == "purging"

        await db_service.set_workspace_purge_status(workspace_id, "purged")
        assert await db_service.get_workspace_purge_status(workspace_id) == "purged"

    @pytest.mark.asyncio
    async def test_set_purge_status_is_idempotent(self, db_service: DatabaseService):
        """Setting the SAME status twice must not raise (upsert, not insert)."""
        workspace_id = _ws("idempotent")

        await db_service.set_workspace_purge_status(workspace_id, "purging")
        await db_service.set_workspace_purge_status(workspace_id, "purging")

        assert await db_service.get_workspace_purge_status(workspace_id) == "purging"

    @pytest.mark.asyncio
    async def test_rejects_invalid_status(self, db_service: DatabaseService):
        with pytest.raises(ValueError, match="Invalid purge status"):
            await db_service.set_workspace_purge_status(_ws("bad"), "deleted")


class TestDeleteWorkspaceSideTables:
    @pytest.mark.asyncio
    async def test_deletes_dead_letter_and_ingestion_events(self, db_service: DatabaseService):
        workspace_id = _ws("side_tables")

        await db_service.add_dead_letter_job(
            document_id="test_doc_dl",
            workspace_id=workspace_id,
            user_id="test_user",
            workflow_run_id="test_run",
            original_message={},
            error_message="boom",
            error_type="ValueError",
        )
        await db_service.record_ingestion_event(
            workflow_run_id="test_run",
            document_id="test_doc_dl",
            event_type="fetch",
            status="failed",
            workspace_id=workspace_id,
        )

        before = await db_service.count_workspace_residue(workspace_id)
        assert before["dead_letter_jobs"] == 1
        assert before["ingestion_events"] == 1

        deleted = await db_service.delete_workspace_side_tables(workspace_id)
        assert deleted["dead_letter_jobs"] == 1
        assert deleted["ingestion_events"] == 1

        after = await db_service.count_workspace_residue(workspace_id)
        assert after["dead_letter_jobs"] == 0
        assert after["ingestion_events"] == 0

    @pytest.mark.asyncio
    async def test_delete_side_tables_is_idempotent(self, db_service: DatabaseService):
        workspace_id = _ws("side_tables_repeat")

        await db_service.add_dead_letter_job(
            document_id="test_doc_dl2",
            workspace_id=workspace_id,
            user_id="test_user",
            workflow_run_id="test_run2",
            original_message={},
            error_message="boom",
            error_type="ValueError",
        )

        first = await db_service.delete_workspace_side_tables(workspace_id)
        second = await db_service.delete_workspace_side_tables(workspace_id)

        assert first["dead_letter_jobs"] == 1
        assert second["dead_letter_jobs"] == 0


class TestRevokeWorkspaceApiKeys:
    @pytest.mark.asyncio
    async def test_revokes_active_keys_only(self, db_service: DatabaseService):
        workspace_id = _ws("keys")

        with db_service.get_session() as session:
            session.execute(
                db_service.api_keys.insert().values(
                    key_id="test_key_active",
                    key_hash="hash1",
                    key_prefix="abcd1234",
                    user_id="test_user",
                    workspace_id=workspace_id,
                    name="active key",
                    status="active",
                    permissions=["read"],
                )
            )
            session.execute(
                db_service.api_keys.insert().values(
                    key_id="test_key_already_revoked",
                    key_hash="hash2",
                    key_prefix="efgh5678",
                    user_id="test_user",
                    workspace_id=workspace_id,
                    name="already revoked",
                    status="revoked",
                    permissions=["read"],
                )
            )

        revoked = await db_service.revoke_workspace_api_keys(workspace_id)
        assert revoked == 1  # only the one that was still active

        # Idempotent: nothing left to revoke on a second run.
        assert await db_service.revoke_workspace_api_keys(workspace_id) == 0

        residue = await db_service.count_workspace_residue(workspace_id)
        assert residue["api_keys_active"] == 0


class TestCountWorkspaceResidueDetectsRealData:
    @pytest.mark.asyncio
    async def test_zero_for_a_workspace_with_no_data(self, db_service: DatabaseService):
        residue = await db_service.count_workspace_residue(_ws("empty"))
        assert all(count == 0 for count in residue.values())

    @pytest.mark.asyncio
    async def test_nonzero_after_upserting_workspace_metadata(self, db_service: DatabaseService):
        """Verification must detect residue, not just always report zero."""
        workspace_id = _ws("residue")
        await db_service.upsert_tenant("test_user_residue")
        await db_service.upsert_workspace_metadata(workspace_id, "test_user_residue")

        residue = await db_service.count_workspace_residue(workspace_id)
        assert residue["workspace_metadata"] == 1

        await db_service.delete_workspace_data(workspace_id)

        residue_after = await db_service.count_workspace_residue(workspace_id)
        assert residue_after["workspace_metadata"] == 0


class TestPurgeReceipt:
    @pytest.mark.asyncio
    async def test_record_and_read_back_receipt(self, db_service: DatabaseService):
        workspace_id = _ws("receipt")
        purge_workflow_id = f"purge-{workspace_id}"

        await db_service.record_purge_receipt(
            purge_workflow_id=purge_workflow_id,
            workspace_id=workspace_id,
            operator="operator@example.com",
            retain_audit_logs=False,
            counts_before={"processed_documents": 3},
            counts_after={"processed_documents": 0},
            verified=True,
        )

        receipt = await db_service.get_purge_receipt(workspace_id)
        assert receipt is not None
        assert receipt["verified"] is True
        assert receipt["operator"] == "operator@example.com"
        # Content-free: every stored value is an id/flag/count, never document text.
        assert receipt["counts_before"] == {"processed_documents": 3}
        assert receipt["counts_after"] == {"processed_documents": 0}

    @pytest.mark.asyncio
    async def test_recording_twice_for_the_same_run_upserts(self, db_service: DatabaseService):
        """Re-running the receipt activity for the SAME purge workflow id must update, not duplicate."""
        workspace_id = _ws("receipt_upsert")
        purge_workflow_id = f"purge-{workspace_id}"

        await db_service.record_purge_receipt(
            purge_workflow_id=purge_workflow_id,
            workspace_id=workspace_id,
            operator="op1",
            retain_audit_logs=False,
            counts_before={"a": 1},
            counts_after={"a": 1},
            verified=False,
        )
        await db_service.record_purge_receipt(
            purge_workflow_id=purge_workflow_id,
            workspace_id=workspace_id,
            operator="op1",
            retain_audit_logs=False,
            counts_before={"a": 1},
            counts_after={"a": 0},
            verified=True,
        )

        receipt = await db_service.get_purge_receipt_by_workflow_id(purge_workflow_id)
        assert receipt["verified"] is True
        assert receipt["counts_after"] == {"a": 0}
