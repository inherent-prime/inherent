-- Migration 012: Add the fk_workspace_tenant foreign key that never got created (#26)
--
-- 000_initial_schema pre-creates workspace_metadata WITHOUT the FK, so
-- 002_multi_tenancy's `CREATE TABLE IF NOT EXISTS ... CONSTRAINT fk_workspace_tenant`
-- was a no-op on every fresh DB and the constraint was silently absent. Without
-- it, deleting a tenant leaves orphaned workspace_metadata rows (no cascade) and
-- a workspace can reference a non-existent tenant.
--
-- Add it idempotently (guarded on pg_constraint) so this is safe to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_workspace_tenant'
    ) THEN
        ALTER TABLE workspace_metadata
            ADD CONSTRAINT fk_workspace_tenant
            FOREIGN KEY (user_id)
            REFERENCES tenants(user_id)
            ON DELETE CASCADE;
    END IF;
END$$;
