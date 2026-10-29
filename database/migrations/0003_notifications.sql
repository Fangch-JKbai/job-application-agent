CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES app_users(id),
    request_id UUID NOT NULL,
    request_fingerprint TEXT NOT NULL,
    text TEXT NOT NULL CHECK (btrim(text) <> ''),
    source TEXT NOT NULL CHECK (source IN ('manual','email','sms','other')),
    source_reference TEXT,
    sent_at TIMESTAMPTZ,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    timezone_hint TEXT,
    hints JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(hints) = 'object'),
    UNIQUE(owner_id,id),
    UNIQUE(owner_id,request_id)
);

CREATE INDEX notifications_owner_time_idx ON notifications(owner_id,imported_at DESC,id);

CREATE FUNCTION reject_notification_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '原始通知不可修改，请另存新通知' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER notifications_original_immutable
BEFORE UPDATE OR DELETE ON notifications
FOR EACH ROW EXECUTE FUNCTION reject_notification_rewrite();

CREATE TABLE notification_extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    notification_id UUID NOT NULL,
    request_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
    provider TEXT,
    model TEXT,
    prompt_version TEXT NOT NULL,
    raw_output TEXT,
    model_result JSONB,
    reviewed_result JSONB,
    review_text TEXT,
    usage JSONB,
    error_status INTEGER,
    error_message TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
    UNIQUE(owner_id,request_id),
    CHECK(model_result IS NULL OR jsonb_typeof(model_result) = 'object'),
    CHECK(reviewed_result IS NULL OR jsonb_typeof(reviewed_result) = 'object')
);

CREATE INDEX notification_extractions_history_idx
ON notification_extractions(owner_id,notification_id,created_at DESC,id);
