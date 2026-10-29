ALTER TABLE jobs ADD COLUMN input_key TEXT NOT NULL DEFAULT '0';
ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN run_token UUID;
ALTER TABLE jobs ADD COLUMN worker_id TEXT;
ALTER TABLE jobs ADD COLUMN lease_until TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE jobs ADD COLUMN deadline_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '15 minutes';
ALTER TABLE jobs ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE jobs ADD COLUMN error_kind TEXT;
ALTER TABLE jobs ADD COLUMN retryable BOOLEAN NOT NULL DEFAULT false;
UPDATE jobs j SET input_key=CASE WHEN input_version=0 THEN '0' ELSE coalesce((SELECT e.id::text||':'||e.version FROM notification_extractions e
 WHERE e.owner_id=j.owner_id AND e.notification_id=j.notification_id AND e.version=j.input_version AND e.created_at<=j.created_at
 ORDER BY e.created_at DESC,e.id DESC LIMIT 1),'legacy-version:'||input_version) END;
ALTER TABLE jobs DROP CONSTRAINT jobs_owner_id_notification_id_operation_input_version_key;
ALTER TABLE jobs ADD UNIQUE(owner_id,notification_id,operation,input_key);
-- P11.1 running calls have unknown outcomes: retain them for explicit reconciliation.
UPDATE jobs SET status='failed',error_kind='legacy_interrupted',error='升级前的执行结果未知，请查看通知后按需重试',finished_at=now() WHERE status='running';
CREATE TABLE job_attempts (
 id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES jobs(id), number INTEGER NOT NULL,
 worker_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','expired')),
 started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ, error_kind TEXT,
 UNIQUE(job_id,number)
);
ALTER TABLE notification_extractions ADD COLUMN job_id UUID REFERENCES jobs(id);
ALTER TABLE notification_extractions ADD COLUMN job_token UUID;
ALTER TABLE notification_extractions ADD COLUMN retryable BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE notification_extractions ADD COLUMN retry_after_seconds DOUBLE PRECISION;
CREATE INDEX jobs_lease_expiry ON jobs(lease_until) WHERE status='running';
CREATE INDEX jobs_available ON jobs(available_at,created_at) WHERE status='queued';
