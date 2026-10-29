-- P11.1 durable single-worker queue. No automatic takeover until P11.2.
CREATE TABLE jobs (
 id UUID PRIMARY KEY,
 owner_id UUID NOT NULL REFERENCES app_users(id),
 notification_id UUID NOT NULL,
 FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
 operation TEXT NOT NULL CHECK(operation='process_notification'),
 input_version INTEGER NOT NULL CHECK(input_version>=0),
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 started_at TIMESTAMPTZ,
 finished_at TIMESTAMPTZ,
 error TEXT,
 result JSONB,
 UNIQUE(owner_id,notification_id,operation,input_version)
);
CREATE INDEX jobs_queued ON jobs(created_at,id) WHERE status='queued';
CREATE INDEX jobs_owner_list ON jobs(owner_id,created_at DESC,id DESC);
CREATE TABLE job_requests (
 owner_id UUID NOT NULL REFERENCES app_users(id),
 request_id UUID NOT NULL,
 fingerprint TEXT NOT NULL,
 job_id UUID NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(owner_id,request_id)
);
