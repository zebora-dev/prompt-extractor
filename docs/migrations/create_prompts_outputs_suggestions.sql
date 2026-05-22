-- Migration: create prompts_outputs_suggestions table
-- Run this against your Supabase project before deploying the google-ai-overview
-- or google-ai-mode extraction flows with PAA (People Also Ask) capture enabled.

create table if not exists prompts_outputs_suggestions (
  id          bigint generated always as identity primary key,
  output_id   bigint       not null references prompts_outputs(id) on delete cascade,
  prompt_id   uuid         not null references prompts(id) on delete cascade,
  brand_id    uuid         not null references brands(id) on delete cascade,
  batch_id    uuid         references batches(id) on delete set null,

  -- The PAA question text (the "People also ask" question)
  index       integer      not null,
  text        text         not null,

  -- The expanded answer captured after clicking the accordion and Show more
  response    text,

  -- Array of source links extracted from the answer panel
  sources     jsonb,

  -- Raw outer HTML of the expanded answer panel (for debugging / re-processing)
  raw_html    text,

  -- Which runner produced this (google-ai-mode, google-ai-overview, etc.)
  llm_model   text,

  -- How the content was captured (paa_dom_expanded, paa_expand_timeout, etc.)
  capture_method text,

  -- Non-null when the item failed to expand or extract
  error       text,

  -- Structured capture metadata (paa_total, paa_capture_method, etc.)
  metadata    jsonb,

  created_at  timestamptz  not null default now()
);

-- Indexes for common lookup patterns
create index if not exists prompts_outputs_suggestions_output_id_idx
  on prompts_outputs_suggestions (output_id);

create index if not exists prompts_outputs_suggestions_batch_id_idx
  on prompts_outputs_suggestions (batch_id);

create index if not exists prompts_outputs_suggestions_brand_id_idx
  on prompts_outputs_suggestions (brand_id);

create index if not exists prompts_outputs_suggestions_prompt_id_idx
  on prompts_outputs_suggestions (prompt_id);

-- Enable Row Level Security (match the pattern of the other tables in this project)
alter table prompts_outputs_suggestions enable row level security;

-- Allow authenticated users full access (adjust to match your existing RLS policies)
create policy "Authenticated users can manage suggestions"
  on prompts_outputs_suggestions
  for all
  to authenticated
  using (true)
  with check (true);
