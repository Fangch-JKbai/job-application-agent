ALTER TABLE action_proposals DROP CONSTRAINT action_proposals_status_check;
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_status_check
    CHECK(status IN ('draft','superseded','confirmed'));
ALTER TABLE agent_sessions DROP CONSTRAINT agent_sessions_status_check;
ALTER TABLE agent_sessions ADD CONSTRAINT agent_sessions_status_check
    CHECK(status IN ('idle','processing','awaiting_user','ready','failed','saved'));

CREATE TABLE proposal_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    notification_id UUID NOT NULL,
    application_id UUID NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(owner_id,proposal_id) REFERENCES action_proposals(owner_id,id),
    FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
    FOREIGN KEY(owner_id,application_id) REFERENCES applications(owner_id,id),
    UNIQUE(owner_id,id),
    UNIQUE(owner_id,proposal_id),
    UNIQUE(owner_id,notification_id)
);

CREATE TABLE confirmation_requests (
    owner_id UUID NOT NULL,
    request_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id,request_id),
    FOREIGN KEY(owner_id,execution_id) REFERENCES proposal_executions(owner_id,id)
);

-- A confirmation always refers to the exact draft the user reviewed.
CREATE FUNCTION reject_proposal_payload_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.owner_id <> OLD.owner_id
        OR NEW.session_id <> OLD.session_id OR NEW.turn_id <> OLD.turn_id THEN
        RAISE EXCEPTION '方案内容不可改写，请生成新方案' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER action_proposals_payload_immutable BEFORE UPDATE ON action_proposals
FOR EACH ROW EXECUTE FUNCTION reject_proposal_payload_rewrite();
