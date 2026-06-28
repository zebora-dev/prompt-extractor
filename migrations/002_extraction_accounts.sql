-- Extraction accounts pool for dynamic Chrome profile management.
--
-- Workers claim an available account at startup, download its Chrome profile
-- from Tigris object storage, and release (re-upload) the profile on exit.
-- Claims older than 2 hours are auto-expired at claim time, covering hard
-- machine failures where the EXIT trap cannot fire.

CREATE TABLE IF NOT EXISTS extraction_accounts (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    email       TEXT        NOT NULL,
    platform    TEXT        NOT NULL DEFAULT 'chatgpt',  -- chatgpt | claude | google | perplexity
    profile_key TEXT        NOT NULL,  -- S3 key: chatgpt/cleo@example.com.tar.gz
    status      TEXT        NOT NULL DEFAULT 'available',  -- available | in_use
    claimed_by  TEXT,        -- FLY_MACHINE_ID of the worker holding this account
    claimed_at  TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (email, platform)
);

CREATE INDEX IF NOT EXISTS idx_extraction_accounts_status_platform
    ON extraction_accounts (status, platform);
