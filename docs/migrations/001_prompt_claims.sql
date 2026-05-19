-- Migration: 001_prompt_claims
-- Purpose: Add prompt_claims table and try_claim_prompt RPC for atomic per-prompt
--          locking. Prevents concurrent workers from processing the same prompt.
--
-- Run this in the Supabase SQL editor before deploying workers that use claiming.
--
-- Table behaviour:
--   status='pending'  — prompt is actively being processed by worker_id
--   status='failed'   — processing failed; prompt is available to be retried
--   (no 'completed')  — on success the claim row is deleted; prompts_outputs is
--                       the source of truth for what has been processed
--
-- Expiry: a pending claim that passes its expires_at is treated as unclaimed by
--   the next worker. The default TTL is 20 minutes — well above the worst-case
--   extraction time. This ensures a crashed/stalled worker releases its lock
--   automatically without manual intervention.

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. Table
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS prompt_claims (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  prompt_id     uuid        NOT NULL,
  batch_id      uuid        NOT NULL,
  brand_id      uuid,
  llm_model     text        NOT NULL,
  worker_id     text        NOT NULL,
  status        text        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'failed')),
  claimed_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '20 minutes',
  error_message text,

  -- One active claim per (prompt, batch, model) at a time.
  CONSTRAINT prompt_claims_unique UNIQUE (prompt_id, batch_id, llm_model)
);

-- Fast lookup when filtering remaining prompts by batch + model + status.
CREATE INDEX IF NOT EXISTS prompt_claims_batch_idx
  ON prompt_claims (batch_id, llm_model, status, expires_at);

-- Fast lookup when claiming or releasing a single prompt.
CREATE INDEX IF NOT EXISTS prompt_claims_prompt_idx
  ON prompt_claims (prompt_id, batch_id, llm_model);

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. Atomic claim function
-- ──────────────────────────────────────────────────────────────────────────────
-- try_claim_prompt attempts to INSERT a pending claim for the given prompt.
-- If a claim already exists it is overwritten ONLY when it is either:
--   • status = 'failed'  (previous run errored — safe to retry)
--   • expires_at < now() (previous worker crashed/timed out — safe to take over)
-- In all other cases (a live pending claim from another worker) the row is left
-- untouched and the function returns FALSE so the caller skips the prompt.

CREATE OR REPLACE FUNCTION try_claim_prompt(
  p_prompt_id   uuid,
  p_batch_id    uuid,
  p_brand_id    uuid,
  p_llm_model   text,
  p_worker_id   text,
  p_ttl_minutes int DEFAULT 20
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO prompt_claims
        (prompt_id, batch_id, brand_id, llm_model, worker_id, status, expires_at)
  VALUES
        (p_prompt_id, p_batch_id, p_brand_id, p_llm_model, p_worker_id,
         'pending', now() + (p_ttl_minutes || ' minutes')::interval)
  ON CONFLICT (prompt_id, batch_id, llm_model) DO UPDATE
    SET worker_id     = EXCLUDED.worker_id,
        brand_id      = EXCLUDED.brand_id,
        status        = 'pending',
        claimed_at    = now(),
        expires_at    = EXCLUDED.expires_at,
        error_message = NULL
    -- Only overwrite when the existing claim is no longer active
    WHERE prompt_claims.status      = 'failed'
       OR prompt_claims.expires_at  < now();

  -- Return TRUE only if we now hold an active pending claim
  RETURN EXISTS (
    SELECT 1
    FROM   prompt_claims
    WHERE  prompt_id  = p_prompt_id
      AND  batch_id   = p_batch_id
      AND  llm_model  = p_llm_model
      AND  worker_id  = p_worker_id
      AND  status     = 'pending'
  );
END;
$$;

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. Row-level security
-- ──────────────────────────────────────────────────────────────────────────────
-- Allow the anon key used by workers to read, insert, update, and delete claims.
-- Adjust the policy names / roles to match your Supabase project's RLS setup.

ALTER TABLE prompt_claims ENABLE ROW LEVEL SECURITY;

-- Workers read claims to check the remaining-prompts list.
CREATE POLICY "prompt_claims_select"
  ON prompt_claims FOR SELECT
  USING (true);

-- Workers insert new claims via the try_claim_prompt RPC.
CREATE POLICY "prompt_claims_insert"
  ON prompt_claims FOR INSERT
  WITH CHECK (true);

-- Workers update claims to mark them failed.
CREATE POLICY "prompt_claims_update"
  ON prompt_claims FOR UPDATE
  USING (true);

-- Workers delete claims after successful processing.
CREATE POLICY "prompt_claims_delete"
  ON prompt_claims FOR DELETE
  USING (true);
