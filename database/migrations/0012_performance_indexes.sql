-- P11.5 measured 5k/10k list sorts and 50k terminal-job admission scan.
CREATE INDEX applications_owner_created_idx ON applications(owner_id,created_at DESC,id DESC);
CREATE INDEX tasks_owner_created_idx ON tasks(owner_id,created_at DESC,id DESC);
CREATE INDEX jobs_active_owner_idx ON jobs(owner_id) WHERE status IN ('queued','running');
