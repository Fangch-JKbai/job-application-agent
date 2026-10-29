ALTER TABLE job_requests ADD COLUMN http_request_id TEXT;
ALTER TABLE job_requests ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX job_requests_job_idx ON job_requests(job_id,created_at);
ALTER TABLE mail_sync_jobs ADD COLUMN origin_request_id TEXT;
CREATE TABLE runtime_instances (
 id UUID PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('api','worker','scheduler')),
 worker_key TEXT, started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), status TEXT NOT NULL DEFAULT 'running',
 pool JSONB NOT NULL DEFAULT '{}',http JSONB NOT NULL DEFAULT '[]'
);
CREATE TABLE operational_alerts (
 owner_id UUID NOT NULL REFERENCES app_users(id), code TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','resolved')),
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 resolved_at TIMESTAMPTZ, episodes INTEGER NOT NULL DEFAULT 1, details JSONB NOT NULL,
 PRIMARY KEY(owner_id,code)
);
