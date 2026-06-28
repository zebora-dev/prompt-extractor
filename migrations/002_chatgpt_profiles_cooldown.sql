-- migrations/002_chatgpt_profiles_cooldown.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds cooldown tracking to chatgpt_profiles.
--
-- When a worker detects a rate-limit or "too many requests" modal it sets
-- cooldown_until so the account is skipped by acquire_chatgpt_profile until
-- the cooldown expires.  Workers can also set cooldown_reason for diagnostics.
--
-- Apply via Supabase SQL editor (service role) or psql.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE chatgpt_profiles
    ADD COLUMN IF NOT EXISTS cooldown_until  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cooldown_reason TEXT;  -- 'rate_limit' | 'login_expired' | 'cloudflare'

-- Update acquire_chatgpt_profile to skip cooled-down accounts.
CREATE OR REPLACE FUNCTION acquire_chatgpt_profile(
    p_worker_id   TEXT,
    p_lock_hours  FLOAT DEFAULT 4.0
)
RETURNS TABLE(profile_index INTEGER, profile_email TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_index  INTEGER;
    v_email  TEXT;
BEGIN
    SELECT cp."index", cp.email
    INTO   v_index, v_email
    FROM   chatgpt_profiles cp
    WHERE  (
               NOT cp.is_locked
            OR cp.lock_expires_at < NOW()
            OR cp.locked_by = p_worker_id
           )
      AND  (cp.cooldown_until IS NULL OR cp.cooldown_until < NOW())
    ORDER BY cp.last_uploaded_at NULLS FIRST, cp."index" ASC
    LIMIT  1
    FOR UPDATE SKIP LOCKED;

    IF v_index IS NULL THEN
        RETURN;
    END IF;

    UPDATE chatgpt_profiles
    SET is_locked       = TRUE,
        locked_by       = p_worker_id,
        locked_at       = NOW(),
        lock_expires_at = NOW() + (p_lock_hours || ' hours')::INTERVAL
    WHERE "index" = v_index;

    profile_index := v_index;
    profile_email := v_email;
    RETURN NEXT;
END;
$$;

-- Helper: set cooldown on a profile (called by workers on rate-limit detection).
CREATE OR REPLACE FUNCTION set_chatgpt_profile_cooldown(
    p_index          INTEGER,
    p_worker_id      TEXT,
    p_cooldown_hours FLOAT DEFAULT 2.0,
    p_reason         TEXT  DEFAULT 'rate_limit'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE chatgpt_profiles
    SET cooldown_until  = NOW() + (p_cooldown_hours || ' hours')::INTERVAL,
        cooldown_reason = p_reason
    WHERE "index"    = p_index
      AND locked_by  = p_worker_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- Helper: manually clear a cooldown (for ops/debugging).
CREATE OR REPLACE FUNCTION clear_chatgpt_profile_cooldown(p_index INTEGER)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE chatgpt_profiles
    SET cooldown_until  = NULL,
        cooldown_reason = NULL
    WHERE "index" = p_index;
END;
$$;
