-- migrations/001_chatgpt_profiles.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Creates the chatgpt_profiles table and supporting Postgres functions used by
-- the dynamic Chrome-profile locking system.
--
-- Apply via Supabase SQL editor → "Run" (you must be using the service role).
-- Or via psql:
--   psql "$DATABASE_URL" -f migrations/001_chatgpt_profiles.sql
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Table ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chatgpt_profiles (
    id               SERIAL      PRIMARY KEY,
    "index"          INTEGER     NOT NULL UNIQUE,
    email            TEXT        NOT NULL,
    is_locked        BOOLEAN     NOT NULL DEFAULT FALSE,
    locked_by        TEXT,              -- FLY_MACHINE_ID / hostname of the holder
    locked_at        TIMESTAMPTZ,
    lock_expires_at  TIMESTAMPTZ,       -- safety valve: auto-expire after N hours
    last_uploaded_at TIMESTAMPTZ,       -- when profile was last snapshotted to Storage
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Seed data ─────────────────────────────────────────────────────────────────
-- 9 ChatGPT accounts → 9 Chrome profile slots.
-- ON CONFLICT DO NOTHING makes this idempotent (safe to re-run).

INSERT INTO chatgpt_profiles ("index", email) VALUES
    (0, 'dev@theround.com'),
    (1, 'chris@theround.com'),
    (2, 'bob@theround.com'),
    (3, 'frank@theround.com'),
    (4, 'info@zebora.io'),
    (5, 'dev@zebora.io'),
    (6, 'data@zebora.io'),
    (7, 'rob@zebora.io'),
    (8, 'john@zebora.io')
ON CONFLICT ("index") DO NOTHING;

-- ── RPC: acquire_chatgpt_profile ──────────────────────────────────────────────
-- Atomically grab an available profile slot.
-- Availability = not locked, OR lock has expired, OR locked by the same worker
-- (covers machine restarts without having to wait for expiry).
-- Uses SELECT … FOR UPDATE SKIP LOCKED so two concurrent callers never collide.
-- Returns one row (profile_index, profile_email) or zero rows if all are taken.

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
    WHERE  NOT cp.is_locked               -- unlocked
       OR  cp.lock_expires_at < NOW()     -- lock expired
       OR  cp.locked_by = p_worker_id     -- same worker re-acquiring (restart)
    ORDER BY cp.last_uploaded_at NULLS FIRST, cp."index" ASC
    LIMIT  1
    FOR UPDATE SKIP LOCKED;

    IF v_index IS NULL THEN
        RETURN;  -- all profiles currently locked by other workers
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

-- ── RPC: release_chatgpt_profile ──────────────────────────────────────────────
-- Release the lock on a profile (called at machine shutdown or on error).
-- Only the holder can release its own lock.

CREATE OR REPLACE FUNCTION release_chatgpt_profile(
    p_index     INTEGER,
    p_worker_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE chatgpt_profiles
    SET is_locked       = FALSE,
        locked_by       = NULL,
        locked_at       = NULL,
        lock_expires_at = NULL
    WHERE "index"   = p_index
      AND locked_by = p_worker_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- ── RPC: refresh_chatgpt_profile_lock ─────────────────────────────────────────
-- Extend the lock expiry and record the upload timestamp.
-- Called after each Phase-3 profile snapshot upload so the machine's hold
-- remains valid even on long extraction runs.

CREATE OR REPLACE FUNCTION refresh_chatgpt_profile_lock(
    p_index      INTEGER,
    p_worker_id  TEXT,
    p_lock_hours FLOAT DEFAULT 4.0
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
