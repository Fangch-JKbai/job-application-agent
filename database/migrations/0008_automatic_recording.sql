ALTER TABLE applications ALTER COLUMN position_name DROP NOT NULL;
CREATE TABLE automatic_records (
 owner_id UUID NOT NULL, notification_id UUID NOT NULL, extraction_version INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('saved','unresolved','ignored','undone')),
 application_id UUID, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(owner_id,notification_id),
 FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
 FOREIGN KEY(owner_id,application_id) REFERENCES applications(owner_id,id)
);
