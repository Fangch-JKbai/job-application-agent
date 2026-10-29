CREATE TABLE recovery_state (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
 paused BOOLEAN NOT NULL DEFAULT false,
 backup_sha256 TEXT,
 restored_at TIMESTAMPTZ,
 verified_at TIMESTAMPTZ,
 activated_at TIMESTAMPTZ
);
INSERT INTO recovery_state(singleton) VALUES(true);
