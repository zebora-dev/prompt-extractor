-- migrations/003_profile_quality_aware_claiming.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Two improvements to the ChatGPT profile pool:
--
-- Opt 3 — Lock TTL reduced from 4h → 1.5h
--   Locks are refreshed every ~30 min during active use, so 1.5h gives 3×
--   headroom for a stall while still releasing orphaned locks (crashed machines)
--   far sooner than the original 4h default.
--
-- Opt 2 — Quality-aware profile claiming
--   Adds lifetime gpt-5-5 / total output counters per profile.  A second
--   acquire function (acquire_chatgpt_profile_quality) orders by success rate
--   so high-quality accounts are preferred over equally-rested low-quality ones.
--   New accounts default to neutral (0.5) so they're still tried early.
--   A companion RPC (update_profile_session_stats) is called by extraction.py
--   at session end to keep the counters current.
--
-- Apply via Supabase SQL editor (service role) or psql.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Opt 2: Add lifetime quality counters ─────────────────────────────────────

ALTER TABLE chatgpt_profiles
    ADD COLUMN IF NOT EXISTS gpt55_lifetime_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_lifetime_count  INTEGER NOT NULL DEFAULT 0;

-- ── Opt 3: Reduce default lock TTL to 1.5 h ──────────────────────────────────

CREATE OR REPLACE FUNCTION acquire_chatgpt_profile(
    p_worker_id   TEXT,
    p_lock_hours  FLOAT DEFAULT 1.5          -- was 4.0
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

CREATE OR REPLACE FUNCTION refresh_chatgpt_profile_lock(
    p_index      INTEGER,
    p_worker_id  TEXT,
    p_lock_hours FLOAT DEFAULT 1.5          -- was 4.0
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE chatgpt_profiles
    SET lock_expires_at  = NOW() + (p_lock_hours || ' hours')::INTERVAL,
        last_uploaded_at = NOW()
    WHERE "index"    = p_index
      AND locked_by  = p_worker_id
      AND is_locked  = TRUE;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- ── Opt 2: Quality-aware acquire RPC ─────────────────────────────────────────
-- Orders by:
--   1. New accounts (last_uploaded_at IS NULL) first — unknown rate, worth trying
--   2. Among rested accounts: highest gpt55_success_rate first
--   3. Tiebreak: LRU (last_uploaded_at ASC)
--
-- gpt55_success_rate is computed inline as:
--   0.5  when total_lifetime_count = 0  (neutral default for new accounts)
--   gpt55_lifetime_count / total_lifetime_count  otherwise

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
        CASE WHEN cp.last_uploaded_at IS NULL THEN 0 ELSE 1 END ASC,
        CASE WHEN cp.total_lifetime_count = 0 THEN 0.5
             ELSE cp.gpt55_lifetime_count::FLOAT / cp.total_lifetime_count
        END DESC,
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

-- ── Opt 2: Update lifetime stats after each session ───────────────────────────
-- Called by extraction.py at session end (normal completion, cap, or downgrade).
-- Increments lifetime counters atomically — safe for concurrent callers.

CREATE OR REPLACE FUNCTION update_profile_session_stats(
    p_index       INTEGER,
    p_worker_id   TEXT,
    p_gpt55_count INTEGER,
    p_total_count INTEGER
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    -- Only update if this worker still holds the lock (guards against
    -- a race where the lock was released and re-acquired by another worker).
    UPDATE chatgpt_profiles
    SET gpt55_lifetime_count = gpt55_lifetime_count + p_gpt55_count,
        total_lifetime_count  = total_lifetime_count  + p_total_count
    WHERE "index"    = p_index
      AND locked_by  = p_worker_id;
END;
$$;
