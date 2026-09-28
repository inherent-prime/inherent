"""PurgeWorkspaceWorkflow (inherent#395): purge everything a workspace left behind.

Operator-triggered, idempotent. Steps run in a fixed order that matters:

1. `mark_workspace_purging` -- stop new writes FIRST. `ensure_tenant_ready`
   (the ingestion workflow's own first activity) checks this marker and
   refuses to prepare tenant infrastructure for a workspace already
   purging, so nothing new can land mid-purge.
2. `cancel_inflight_ingestion` -- stop what's already running, so no
   in-flight run can still write AFTER step 3+ below have already counted
   this workspace as empty.
3. Delete everything: Postgres (documents+chunks, dead-letters, ingestion
   events, redaction audit, eval rows, stats ledger, workspace_metadata
   itself), Weaviate (the whole collection), and Mongo audit logs (unless
   `retain_audit_logs` opted out -- see PurgeWorkspaceInput's docstring for
   why the default is to purge).
4. Revoke (not delete) API keys scoped to the workspace.
5. `mark_workspace_purged` -- terminal state.
6. `verify_purge` -- count residue per store. This IS the proof-of-purge
   report the issue asks for.
7. `record_purge_receipt` -- durable, content-free record of the whole run.

Every activity is independently idempotent (see purge.py's module
docstring), so if this workflow is re-run for the same workspace after a
partial failure, every already-completed delete just deletes zero rows the
second time and the workflow still ends in the same verified state.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.temporal.models import PurgeWorkspaceInput, PurgeWorkspaceResult

_DEFAULT_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))


@workflow.defn
class PurgeWorkspaceWorkflow:
    """Purge every store's data for one workspace and return a verification report."""

    @workflow.run
    async def run(self, input: PurgeWorkspaceInput) -> PurgeWorkspaceResult:
        purge_workflow_id = workflow.info().workflow_id

        # 1. Stop new writes first.
        await workflow.execute_activity(
            "mark_workspace_purging",
            input,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )

        # Snapshot residue BEFORE any delete, for the receipt's counts_before.
        counts_before = await workflow.execute_activity(
            "verify_purge",
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DEFAULT_RETRY,
        )

        # 2. Stop what's already running.
        cancelled = await workflow.execute_activity(
            "cancel_inflight_ingestion",
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DEFAULT_RETRY,
        )

        # 3. Delete everything, store by store.
        await workflow.execute_activity(
            "purge_postgres_documents",
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DEFAULT_RETRY,
        )
        await workflow.execute_activity(
            "purge_postgres_side_tables",
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DEFAULT_RETRY,
        )
        await workflow.execute_activity(
            "purge_weaviate_collection",
            input,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_DEFAULT_RETRY,
        )
        await workflow.execute_activity(
            "purge_audit_logs",
            input,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_DEFAULT_RETRY,
        )

        # 4. Revoke (not delete) API keys.
        revoked_keys = await workflow.execute_activity(
            "revoke_workspace_api_keys",
            input,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )

        # 5. Terminal state.
        await workflow.execute_activity(
            "mark_workspace_purged",
            input,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )

        # 6. Verify: this is the proof-of-purge report.
        counts_after = await workflow.execute_activity(
            "verify_purge",
            input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DEFAULT_RETRY,
        )
        verified = all(count <= 0 for count in counts_after.values())

        # 7. Durable, content-free receipt.
        await workflow.execute_activity(
            "record_purge_receipt",
            args=[input, purge_workflow_id, counts_before, counts_after],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )

        return PurgeWorkspaceResult(
            workspace_id=input.workspace_id,
            purge_workflow_id=purge_workflow_id,
            residue=counts_after,
            verified=verified,
            revoked_api_keys=revoked_keys,
            cancelled_ingestion_workflows=cancelled,
            # Ran the delete step (retain_audit_logs is the only reason it
            # would skip) -- not "found rows to delete", which would read
            # False for an already-empty workspace re-run.
            audit_logs_purged=not input.retain_audit_logs,
        )
