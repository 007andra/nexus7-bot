-- NEXUS-7: read-only snapshot of frozen OOS research cohort.
-- Run only in an authorized Postgres SQL client. Export SELECT result privately.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH frozen AS (
  SELECT cohort_id,
         CASE WHEN pg_input_is_valid(payload,'jsonb') THEN payload::jsonb END AS metadata
  FROM prospective_oos_cohort_v1
  WHERE cohort_id = 'CALIBRATION_GENERALIZATION_V1'
    AND (payload::jsonb ->> 'hypothesis_frozen') = 'true'
    AND (payload::jsonb ->> 'reset_allowed') = 'false'
    AND (payload::jsonb ->> 'started_epoch') =
        (payload::jsonb ->> 'discovery_cutoff_epoch')
), candidates AS (
  SELECT f.cohort_id,
    (f.metadata ->> 'started_epoch')::double precision AS started_epoch,
    c.candidate_id, c.symbol, p.signal AS signal,
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
    -- Column captured_epoch is REAL; compare full-precision JSON timestamp.
    AND CASE WHEN pg_input_is_valid(p.signal->>'captured_epoch','double precision')
      THEN (p.signal->>'captured_epoch')::double precision >=
        (f.metadata ->> 'started_epoch')::double precision
      AND (p.signal->>'captured_epoch')::double precision <=
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
  COALESCE(op.outcome ->> 'outcome','MISSING') AS outcome_state,
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
