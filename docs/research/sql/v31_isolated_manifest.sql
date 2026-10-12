-- PROPOSAL ONLY. NEVER executed by the bot or from this PR.
-- Independent DBA/security review required. Only on a dedicated database
-- whose name begins nexus7_research_ (NEVER on the trading database).
-- Admin/owner provisions the genesis head using an authenticated, externally
-- protected #658 cutoff and an independently signed/protected initial digest.
-- This migration intentionally has NO INSERT for genesis and NO automatic GRANT
-- to production runtime/trading roles.
BEGIN;
CREATE SCHEMA IF NOT EXISTS v31_research;
REVOKE ALL ON SCHEMA v31_research FROM PUBLIC;
CREATE TABLE IF NOT EXISTS v31_research.manifest_heads (
    protocol TEXT PRIMARY KEY,
    effective_cutoff_epoch DOUBLE PRECISION NOT NULL CHECK (
        effective_cutoff_epoch > 1791833992 AND
        effective_cutoff_epoch < 4102444800
    ),
    implementation_sha CHAR(40) NOT NULL,
    authorization_reference TEXT NOT NULL,
    genesis_digest CHAR(64) NOT NULL,
    sequence BIGINT NOT NULL DEFAULT 0 CHECK (sequence >= 0),
    head_digest CHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS v31_research.manifest_events (
    protocol TEXT NOT NULL REFERENCES v31_research.manifest_heads(protocol),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    candidate_id TEXT NOT NULL,
    event_data JSONB NOT NULL,
    digest CHAR(64) NOT NULL,
    PRIMARY KEY (protocol, sequence),
    UNIQUE (protocol, candidate_id)
);
-- Protect events even against inadvertently granting UPDATE/DELETE.
CREATE OR REPLACE FUNCTION v31_research.reject_event_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'V31_IMMUTABLE_EVENT_CHANGE_REJECTED';
END;
$$;
DROP TRIGGER IF EXISTS v31_reject_event_mutation ON v31_research.manifest_events;
CREATE TRIGGER v31_reject_event_mutation
BEFORE UPDATE OR DELETE ON v31_research.manifest_events
FOR EACH ROW EXECUTE FUNCTION v31_research.reject_event_mutation();

-- Only sequence and head digest may change; constrain a head update to
-- one committed chain step backed by the corresponding inserted event.
CREATE OR REPLACE FUNCTION v31_research.require_one_step_head()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.protocol IS DISTINCT FROM OLD.protocol
       OR NEW.effective_cutoff_epoch IS DISTINCT FROM OLD.effective_cutoff_epoch
       OR NEW.implementation_sha IS DISTINCT FROM OLD.implementation_sha
       OR NEW.authorization_reference IS DISTINCT FROM OLD.authorization_reference
       OR NEW.genesis_digest IS DISTINCT FROM OLD.genesis_digest
       OR NEW.sequence <> OLD.sequence + 1
       OR NOT EXISTS (
          SELECT 1 FROM v31_research.manifest_events e
          WHERE e.protocol = NEW.protocol
          AND e.sequence = NEW.sequence
          AND e.digest = NEW.head_digest
          AND e.event_data->>'previous_digest' = OLD.head_digest
          AND e.event_data->>'digest' = NEW.head_digest
       )
    THEN
        RAISE EXCEPTION 'V31_MANIFEST_HEAD_UPDATE_REJECTED';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS v31_require_one_step_head ON v31_research.manifest_heads;
CREATE TRIGGER v31_require_one_step_head
BEFORE UPDATE ON v31_research.manifest_heads
FOR EACH ROW EXECUTE FUNCTION v31_research.require_one_step_head();

REVOKE ALL ON ALL TABLES IN SCHEMA v31_research FROM PUBLIC;
-- PRECONDITION: independently provisioned dedicated NON-SUPERUSER role
-- nexus_v31_writer, no membership in trading roles, NO CREATE rights.
-- Run GRANTs only after a separate security review has proven isolation.
-- GRANT USAGE ON SCHEMA v31_research TO nexus_v31_writer;
-- GRANT SELECT ON v31_research.manifest_heads TO nexus_v31_writer;
-- GRANT SELECT, INSERT ON v31_research.manifest_events TO nexus_v31_writer;
-- GRANT UPDATE (sequence,head_digest) ON v31_research.manifest_heads
--   TO nexus_v31_writer;
COMMIT;
-- DO NOT commit migration into the actual production environment.
-- Live capital, HWM, trading orders and all risk gates remain untouched.
