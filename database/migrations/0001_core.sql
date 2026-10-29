-- P2: local identity and three business tables. PostgreSQL 16, no extensions.
CREATE TABLE app_users (
    id UUID PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (btrim(display_name) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES app_users(id),
    company_name TEXT NOT NULL CHECK (btrim(company_name) <> ''),
    company_aliases TEXT[] NOT NULL DEFAULT '{}',
    position_name TEXT NOT NULL CHECK (btrim(position_name) <> ''),
    batch TEXT CHECK (batch IS NULL OR btrim(batch) <> ''),
    stage TEXT NOT NULL DEFAULT 'submitted'
        CHECK (stage IN ('submitted','screening','assessment','written_test','interview','offer')),
    outcome TEXT NOT NULL DEFAULT 'active'
        CHECK (outcome IN ('active','rejected','withdrawn','accepted','closed')),
    applied_at TIMESTAMPTZ,
    applied_on DATE,
    notes TEXT,
    data_origin TEXT NOT NULL DEFAULT 'manual' CHECK (data_origin IN ('manual','synthetic')),
    sample_case_id TEXT,
    sample_key TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, id),
    UNIQUE (owner_id, sample_case_id, sample_key),
    CHECK (applied_at IS NULL OR applied_on IS NULL),
    CHECK ((data_origin = 'manual' AND sample_case_id IS NULL AND sample_key IS NULL)
        OR (data_origin = 'synthetic' AND sample_case_id IS NOT NULL
            AND btrim(sample_case_id) <> '' AND sample_key IS NOT NULL AND btrim(sample_key) <> ''))
);

CREATE INDEX applications_owner_outcome_idx ON applications (owner_id, outcome);
CREATE INDEX applications_owner_company_idx ON applications (owner_id, company_name);

CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    application_id UUID NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('assessment','written_test','interview','material_submission','offer_response','other')),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','completed','cancelled')),
    round INTEGER CHECK (round > 0),
    opens_at TIMESTAMPTZ,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ,
    starts_on DATE,
    due_on DATE,
    source_timezone TEXT CHECK (source_timezone IS NULL OR btrim(source_timezone) <> ''),
    time_note TEXT,
    notes TEXT,
    data_origin TEXT NOT NULL DEFAULT 'manual' CHECK (data_origin IN ('manual','synthetic')),
    sample_case_id TEXT,
    sample_key TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, application_id) REFERENCES applications(owner_id, id),
    UNIQUE (owner_id, application_id, id),
    UNIQUE (owner_id, sample_case_id, sample_key),
    CHECK (starts_at IS NULL OR starts_on IS NULL),
    CHECK (due_at IS NULL OR due_on IS NULL),
    CHECK (starts_at IS NULL OR ends_at IS NULL OR starts_at <= ends_at),
    CHECK (opens_at IS NULL OR due_at IS NULL OR opens_at <= due_at),
    CHECK ((opens_at IS NULL AND starts_at IS NULL AND ends_at IS NULL AND due_at IS NULL)
        OR source_timezone IS NOT NULL),
    CHECK ((data_origin = 'manual' AND sample_case_id IS NULL AND sample_key IS NULL)
        OR (data_origin = 'synthetic' AND sample_case_id IS NOT NULL
            AND btrim(sample_case_id) <> '' AND sample_key IS NOT NULL AND btrim(sample_key) <> ''))
);

CREATE INDEX tasks_owner_application_idx ON tasks (owner_id, application_id);
CREATE INDEX tasks_owner_due_idx ON tasks (owner_id, due_at)
    WHERE status IN ('pending','in_progress');
CREATE INDEX tasks_owner_start_idx ON tasks (owner_id, starts_at);

CREATE TABLE application_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    application_id UUID NOT NULL,
    task_id UUID,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'application_created','application_updated','task_created','task_updated','task_cancelled','user_correction')),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('local_user','system')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('manual','synthetic_seed','user_correction','notification')),
    source_reference TEXT,
    reason TEXT,
    before_data JSONB CHECK (before_data IS NULL OR jsonb_typeof(before_data) = 'object'),
    after_data JSONB NOT NULL CHECK (jsonb_typeof(after_data) = 'object'),
    operation_id UUID NOT NULL,
    event_index INTEGER NOT NULL DEFAULT 0 CHECK (event_index >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, application_id) REFERENCES applications(owner_id, id),
    FOREIGN KEY (owner_id, application_id, task_id) REFERENCES tasks(owner_id, application_id, id),
    UNIQUE (owner_id, operation_id, event_index),
    CHECK ((event_type IN ('application_created','application_updated') AND task_id IS NULL)
        OR (event_type IN ('task_created','task_updated','task_cancelled') AND task_id IS NOT NULL)
        OR event_type = 'user_correction')
);

CREATE INDEX application_events_timeline_idx ON application_events (owner_id, application_id, created_at, id);

-- Corrections append a new event instead of rewriting old evidence.
CREATE FUNCTION reject_event_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'application_events are append-only' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER application_events_append_only
BEFORE UPDATE OR DELETE ON application_events
FOR EACH ROW EXECUTE FUNCTION reject_event_rewrite();
