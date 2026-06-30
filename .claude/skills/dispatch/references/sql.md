# Supabase SQL Reference

Use these queries for read-only status checks. Use `supabase-py` for writes such as lock release.

## Batch Metadata

```sql
SELECT b.id, b.name, b.brand_id, br.name as brand_name,
       b.status, b.llm_models
FROM batches b
LEFT JOIN brands br ON br.id = b.brand_id
WHERE b.id = '<batch_id>';
```

Extract required models from `b.llm_models.required_models` when present.

## Required-Model Progress

```sql
SELECT
  llm_model,
  COUNT(*) AS total_outputs,
  COUNT(DISTINCT prompt_id) AS unique_prompts
FROM prompts_outputs
WHERE batch_id = '<batch_id>'
  AND active = true
  AND llm_model IN ('<model1>', '<model2>')
GROUP BY llm_model
ORDER BY llm_model;
```

Fully complete prompts for two required models:

```sql
WITH m1 AS (
  SELECT DISTINCT prompt_id FROM prompts_outputs
  WHERE batch_id = '<batch_id>' AND active = true AND llm_model = '<model1>'
),
m2 AS (
  SELECT DISTINCT prompt_id FROM prompts_outputs
  WHERE batch_id = '<batch_id>' AND active = true AND llm_model = '<model2>'
)
SELECT COUNT(*) AS fully_complete FROM m1 JOIN m2 USING (prompt_id);
```

For more than two required models, count prompt IDs that have all required models.

## Non-Required-Model Progress

```sql
SELECT llm_model, COUNT(DISTINCT prompt_id) AS done
FROM prompts_outputs
WHERE batch_id = '<batch_id>' AND active = true
GROUP BY llm_model;
```

## Total Prompts

```sql
SELECT COUNT(*) AS total
FROM prompts p
WHERE EXISTS (
  SELECT 1 FROM prompts_outputs po
  WHERE po.prompt_id = p.id AND po.batch_id = '<batch_id>'
);
```

## Google AI Overview Last Output Age

```sql
SELECT
  EXTRACT(EPOCH FROM (NOW() - MAX(run_at))) / 60 AS minutes_since_last_output
FROM prompts_outputs
WHERE batch_id = '<batch_id>'
  AND active = true
  AND llm_model = 'google-ai-overview';
```

If `minutes_since_last_output > 20` and flows are running, report a stall.

## GPT-UK Account Health

```sql
SELECT "index", email, status, worker, cooldown_until, cooldown_reason,
       last24h_gpt55, last24h_mini, last24h_total, last24h_gpt55_pct
FROM chatgpt_profile_stats
ORDER BY last24h_total DESC;
```

Pool summary:

```sql
SELECT
  COUNT(*) FILTER (WHERE NOT is_locked OR locked_by = 'disabled') AS unlocked,
  COUNT(*) FILTER (WHERE is_locked AND locked_by != 'disabled') AS in_use,
  COUNT(*) FILTER (WHERE cooldown_until > NOW()) AS cooling_down,
  COUNT(*) FILTER (WHERE is_locked AND locked_by = 'disabled') AS disabled
FROM chatgpt_profiles;
```

Clear a cooldown only when the operator explicitly requests it:

```sql
SELECT clear_chatgpt_profile_cooldown(<index>);
```
