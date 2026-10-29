-- Recognize an exact retry, and reject reusing its request ID for another edit.
ALTER TABLE application_events ADD COLUMN request_fingerprint TEXT;
