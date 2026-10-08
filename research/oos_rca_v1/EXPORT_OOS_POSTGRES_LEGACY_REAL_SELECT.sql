-- NEXUS-7: read-only REPRODUCTION of legacy OOS REAL-cutoff snapshot.\n-- Do not compare this file against precise JSON maturation-review candidate counts.
-- Run only in an authorized Postgres SQL client. Export SELECT result privately.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH frozen_source AS (
  SELECT cohort_id,
         CASE WHEN pg_input_is_valid(payload,'jsonb') THEN payload::jsonb END AS metadata
  FROM prospective_oos_cohort_v1
  WHERE cohort_id = 'CALIBRATION_GENERALIZATION_V1'
), frozen AS (
  SELECT cohort_id, metadata
  FROM frozen_source
  WHERE metadata ->> 'hypothesis_frozen' = 'true'
    AND metadata ->> 'reset_allowed' = 'false'
    AND metadata ->> 'started_epoch' = metadata ->> 'discovery_cutoff_epoch'
    AND pg_input_is_valid(metadata ->> 'started_epoch', 'double precision')
), candidates AS (
  SELECT f.cohort_id,
    (f.metadata ->> 'started_epoch')::double precision AS started_epoch,
    c.candidate_id, c.symbol, c.captured_epoch::double precision AS captured_epoch_real,
    p.signal AS signal,
    CASE WHEN pg_input_is_valid(p.signal->>'captured_epoch','double precision')
      THEN (p.signal->>'captured_epoch')::double precision END AS capture,
    p.signal -> 'counterfactual_nexus_v1' AS cf
  FROM hard_gate_shadow_candidates_v1 AS c
  CROSS JOIN frozen AS f
  CROSS JOIN LATERAL (
    SELECT CASE WHEN pg_input_is_valid(c.payload,'jsonb')
      THEN c.payload::jsonb END AS signal
  ) AS p
  WHERE c.population = 'HARD_GATE_SHADOW'
    -- Column captured_epoch REAL mirrors old OOS SQL-query population selection.
    -- Deliberately reproduces legacy PostgreSQL REAL snapshot predicate,
    -- not the more precise maternity-review JSON cutoff.
    AND c.captured_epoch >= ((f.metadata ->> 'started_epoch')::real)
    AND CASE WHEN pg_input_is_valid(p.signal->>'captured_epoch','double precision')
      THEN (p.signal->>'captured_epoch')::double precision <=
        EXTRACT(EPOCH FROM transaction_timestamp())::double precision
      ELSE false END
), validated AS (
  SELECT * FROM candidates AS c
  WHERE c.cf ->> 'cohort' = 'MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS'
    AND c.cf ->> 'candidate_id' = c.candidate_id
    AND c.signal ->> 'candidate_id' = c.candidate_id
    AND c.cf ->> 'risk_epoch_traversal_credit' = 'false'
    AND c.signal ->> 'shadow_only' = 'true'
    AND c.signal ->> 'live_eligible' = 'false'
)
SELECT validated.cohort_id,
  EXTRACT(EPOCH FROM transaction_timestamp()) AS snapshot_as_of_epoch,
  validated.started_epoch AS frozen_started_epoch,
  validated.candidate_id, validated.capture AS captured_epoch,
  validated.captured_epoch_real,
  'REAL_COARSE' AS export_scope,
  validated.cf ->> 'status' AS counterfactual_status,
  validated.cf ->> 'execution_allowed' AS execution_allowed_source,
  validated.symbol, validated.signal ->> 'side' AS side,
  validated.signal ->> 'regime' AS regime,
  validated.signal ->> 'setup' AS setup,
  validated.signal ->> 'entry' AS entry_reference,
  validated.signal ->> 'stop' AS stop_reference,
  validated.signal ->> 'target' AS target_reference,
  CASE
    WHEN validated.cf ->> 'execution_allowed' = 'true' THEN 'COUNTERFACTUAL_APPROVED'
    ELSE 'COUNTERFACTUAL_REJECTED'
  END AS decision_state,
  h.horizon AS outcome_horizon_minutes,
  CASE WHEN o.candidate_id IS NOT NULL AND op.outcome IS NULL
    THEN 'MALFORMED_JSON' ELSE COALESCE(op.outcome ->> 'outcome','MISSING') END AS outcome_state,
  o.candidate_id IS NULL OR op.outcome IS NOT NULL AS outcome_payload_parse_ok,
  op.outcome ->> 'return_basis' AS return_basis,
  op.outcome ->> 'future_return' AS hypothetical_gross_return,
  op.outcome ->> 'MFE' AS hypothetical_mfe,
  op.outcome ->> 'MAE' AS hypothetical_mae,
  op.outcome ->> 'observation_start' AS observation_start_epoch,
  CASE
    WHEN o.candidate_id IS NULL THEN true
    ELSE COALESCE(op.outcome ->> 'candidate_id',validated.candidate_id) =
         validated.candidate_id
  END AS outcome_identity_ok,
  CASE
    WHEN o.candidate_id IS NULL THEN true
    ELSE (op.outcome ->> 'horizon') = h.horizon::text
  END AS outcome_horizon_ok
FROM validated
CROSS JOIN (VALUES (60),(240)) AS h(horizon)
LEFT JOIN hard_gate_shadow_outcomes_v1 AS o
  ON o.candidate_id=validated.candidate_id
  AND o.horizon=h.horizon
  AND o.population='HARD_GATE_SHADOW'
CROSS JOIN LATERAL (
  SELECT CASE WHEN pg_input_is_valid(o.payload,'jsonb')
    THEN o.payload::jsonb END AS outcome
) AS op
ORDER BY validated.capture, validated.candidate_id, h.horizon;

ROLLBACK;
