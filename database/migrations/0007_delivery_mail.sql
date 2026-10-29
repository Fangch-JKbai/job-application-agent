ALTER TABLE notifications ADD COLUMN calendar_year INTEGER CHECK(calendar_year BETWEEN 1900 AND 2200);
CREATE TABLE mailbox_accounts (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), owner_id UUID NOT NULL REFERENCES app_users(id),
 address TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('qq','163','126')),
 secret_encrypted TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
 uidvalidity TEXT, last_uid BIGINT NOT NULL DEFAULT 0, last_sync_at TIMESTAMPTZ, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(owner_id,address), UNIQUE(owner_id,id)
);
CREATE TABLE mailbox_messages (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), owner_id UUID NOT NULL,
 account_id UUID NOT NULL, uidvalidity TEXT NOT NULL, uid TEXT NOT NULL,
 subject TEXT NOT NULL, sender TEXT NOT NULL, sent_at TIMESTAMPTZ, text TEXT NOT NULL,
 imported_at TIMESTAMPTZ NOT NULL DEFAULT now(), notification_id UUID, is_candidate BOOLEAN NOT NULL,
 processing_error TEXT, auto_state TEXT NOT NULL CHECK(auto_state IN ('pending','processing','completed','failed','skipped')),
 FOREIGN KEY(owner_id,account_id) REFERENCES mailbox_accounts(owner_id,id) ON DELETE CASCADE,
 FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
 UNIQUE(account_id,uidvalidity,uid), UNIQUE(owner_id,id)
);
CREATE TABLE intake_receipts (
 owner_id UUID NOT NULL REFERENCES app_users(id), request_id UUID NOT NULL,
 notification_id UUID NOT NULL, request_fingerprint TEXT NOT NULL, result JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(owner_id,request_id), UNIQUE(owner_id,notification_id),
 FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id)
);
CREATE TABLE model_admissions (
 owner_id UUID NOT NULL REFERENCES app_users(id), hour TIMESTAMPTZ NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(owner_id,hour)
);

CREATE FUNCTION recruitment_category(value TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE WHEN regexp_replace(lower(value),'\s+','','g') IN ('27秋招','2027秋招') THEN '27秋招'
 WHEN value LIKE '%实习%' THEN '实习' ELSE regexp_replace(lower(value),'\s+','','g') END;
$$;
