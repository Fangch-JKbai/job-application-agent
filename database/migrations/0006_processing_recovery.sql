ALTER TABLE notification_extractions
    ADD COLUMN phase TEXT NOT NULL DEFAULT 'finished',
    ADD COLUMN error_kind TEXT,
    ADD COLUMN elapsed_ms BIGINT,
    ADD COLUMN retry_of UUID REFERENCES notification_extractions(id),
    ADD COLUMN cost_basis JSONB NOT NULL DEFAULT '{"estimated_cost":null,"currency":null,"basis":"未配置计价；仅记录服务商返回的用量，不据此推断账单"}';
UPDATE notification_extractions SET phase=CASE WHEN status='processing' THEN 'legacy_processing' ELSE status END,
    elapsed_ms=CASE WHEN finished_at IS NOT NULL THEN greatest(0,(extract(epoch FROM finished_at-created_at)*1000)::bigint) END;
ALTER TABLE agent_turns
    ADD COLUMN phase TEXT NOT NULL DEFAULT 'finished',
    ADD COLUMN error_kind TEXT,
    ADD COLUMN elapsed_ms BIGINT,
    ADD COLUMN retry_of UUID REFERENCES agent_turns(id),
    ADD COLUMN input_state JSONB,
    ADD COLUMN usage JSONB,
    ADD COLUMN cost_basis JSONB NOT NULL DEFAULT '{"estimated_cost":null,"currency":null,"basis":"未配置计价；仅记录服务商返回的用量，不据此推断账单"}';
UPDATE agent_turns SET phase=CASE WHEN status='processing' THEN 'legacy_processing' ELSE status END,
    elapsed_ms=CASE WHEN finished_at IS NOT NULL THEN greatest(0,(extract(epoch FROM finished_at-created_at)*1000)::bigint) END;
