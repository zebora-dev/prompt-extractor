-- migrations/004_load_balanced_claiming.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Extends acquire_chatgpt_profile_quality (added in 003) with a load-balancing
-- tiebreak so accounts that have processed fewer total prompts are preferred
-- when quality rates are similar.
--
-- Problem: accounts with 0 lifetime outputs (new/unused accounts) were being
-- skipped in favour of overworked accounts that simply had an older
-- last_uploaded_at. The LRU tiebreak in 003 couldn't differentiate between
-- "rested for a while" and "never used".
--
-- Fix: add total_lifetime_count ASC as a secondary sort key after the rate
-- sort. Accounts with total_lifetime_count = 0 already get a neutral 0.5 rate
-- (better than most active accounts at ~20–44%) so they naturally sort to the
-- front. The load-balance tiebreak reinforces this when rates happen to be equal.
--
-- Apply via Supabase SQL Editor (direct port 5432 blocked externally).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION acquire_chatgpt_profile_quality(
    p_worker_id   TEXT,
    p_lock_hours  FLOAT DEFAULT 1.5
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
    ORDER BY
        -- Tier 1: new accounts (no upload history) always first
        CASE WHEN cp.last_uploaded_at IS NULL THEN 0 ELSE 1 END ASC,
        -- Tier 2: highest gpt-5-5 success rate (new/zero-count accounts get neutral 0.5)
        CASE WHEN cp.total_lifetime_count = 0 THEN 0.5
             ELSE cp.gpt55_lifetime_count::FLOAT / cp.total_lifetime_count
        END DESC,
        -- Tier 3: load balance — prefer accounts that have done less total work
        cp.total_lifetime_count ASC,
        -- Tier 4: LRU tiebreak
        cp.last_uploaded_at ASC,
        cp."index" ASC
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
