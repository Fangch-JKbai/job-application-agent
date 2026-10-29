ALTER TABLE mailbox_accounts ADD COLUMN next_sync_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE mailbox_messages ADD COLUMN processing_job_id UUID REFERENCES jobs(id);
CREATE TABLE mail_sync_jobs (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),owner_id UUID NOT NULL,account_id UUID NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed')),
 run_token UUID,lease_until TIMESTAMPTZ,available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,error TEXT,
 FOREIGN KEY(owner_id,account_id) REFERENCES mailbox_accounts(owner_id,id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX mail_one_active_sync ON mail_sync_jobs(account_id) WHERE status IN ('queued','running');
CREATE INDEX mail_sync_due ON mail_sync_jobs(available_at) WHERE status IN ('queued','running');
