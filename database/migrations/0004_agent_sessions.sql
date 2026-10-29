CREATE TABLE agent_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    notification_id UUID NOT NULL,
    extraction_id UUID NOT NULL REFERENCES notification_extractions(id),
    extraction_version INTEGER NOT NULL,
    request_id UUID NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle' CHECK(status IN ('idle','processing','awaiting_user','ready','failed')),
    state JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(owner_id,notification_id) REFERENCES notifications(owner_id,id),
    UNIQUE(owner_id,id),
    UNIQUE(owner_id,request_id)
);

CREATE TABLE agent_turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    session_id UUID NOT NULL,
    request_id UUID NOT NULL,
    request_fingerprint TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply_to_question_id UUID,
    status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
    assistant_text TEXT,
    trace JSONB NOT NULL DEFAULT '[]',
    error_status INTEGER,
    error_message TEXT,
    response_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY(owner_id,session_id) REFERENCES agent_sessions(owner_id,id),
    UNIQUE(owner_id,request_id)
);
CREATE INDEX agent_turns_history_idx ON agent_turns(owner_id,session_id,created_at,id);

CREATE TABLE action_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    session_id UUID NOT NULL,
    turn_id UUID NOT NULL REFERENCES agent_turns(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','superseded')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(owner_id,session_id) REFERENCES agent_sessions(owner_id,id),
    UNIQUE(owner_id,id)
);
CREATE INDEX action_proposals_session_idx ON action_proposals(owner_id,session_id,created_at DESC,id);
