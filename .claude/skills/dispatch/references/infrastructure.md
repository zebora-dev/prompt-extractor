# Infrastructure Reference

Use live lookups when possible because machine IDs and deployment IDs can change.

## Constants

Prefect API:

```text
https://prompt-extractor-prefect.fly.dev/api
```

Extraction type mapping:

| Type | Region | Fly app | Work pool | Deployment name |
|---|---|---|---|---|
| `gpt-uk` | uk | `gpt-extractor-uk` | `gpt-extraction-uk` | `chatgpt-extraction-batch-gpt-uk` |
| `gpt` | us | `prompt-extractor-us` | `prompt-extraction-pool` | `chatgpt-extraction-batch` |
| `google-ai-overview` | us | `prompt-extractor-google-us` | `prompt-extraction-google-us` | `google-ai-overview-extraction-batch-google-us` |
| `google-ai-overview` | uk | `prompt-extractor-google-uk` | `prompt-extraction-google-uk` | `google-ai-overview-extraction-batch-google-uk` |
| `google-ai-mode` | us | `prompt-extractor-google-us` | `prompt-extraction-google-us` | `google-ai-mode-extraction-batch-google-us` |
| `google-ai-mode` | uk | `prompt-extractor-google-uk` | `prompt-extraction-google-uk` | `google-ai-mode-extraction-batch-google-uk` |
| `claude` | uk | `prompt-extractor-uk` | `prompt-extraction-uk` | `claude-extraction-batch-uk` |
| `perplexity` | uk | `prompt-extractor-perplexity-uk` | `prompt-extraction-perplexity-uk` | `perplexity-extraction-batch-uk` |

Known deployment IDs:

| Type | Deployment ID |
|---|---|
| `gpt-uk` | `1b26c690-9142-4424-96ad-f31725816244` |
| `gpt` | `65dc0188-c85b-4940-afac-8c298794c0b5` |
| `google-ai-overview` us | `d1719408-9a21-4f2f-b743-92ee1d5b2756` |
| `google-ai-mode` us | `c2e4b38b-81be-4bc5-86d6-e36da7e28223` |
| `claude` | `88c148ef-957f-4c1c-ac74-19fa4df3bdd4` |
| `perplexity` | `52c0135b-3635-4460-a995-2efc698c1ef4` |

If an ID is stale or missing, resolve by name:

```bash
python .claude/skills/dispatch/scripts/prefect_api.py deployment-id \
  --flow-name "<flow-name>" \
  --deployment-name "<deployment-name>"
```

## Status Commands

List Fly machines:

```bash
python .claude/skills/dispatch/scripts/fly_machines.py list --app <app>
```

Check Prefect workers:

```bash
python .claude/skills/dispatch/scripts/prefect_api.py workers --work-pool <work_pool>
```

List recent deployment flow runs:

```bash
python .claude/skills/dispatch/scripts/prefect_api.py flow-runs --deployment-id <deployment_id> --tracked <id1,id2>
```

Check batch status through the existing project script when available:

```bash
./scripts/batch_status.sh <batch_id>
```

## Machine Mutation

Mutating Fly actions require `--apply`; without it the helper prints the intended action.

```bash
python .claude/skills/dispatch/scripts/fly_machines.py start --app <app> --machines <m1,m2> --apply
python .claude/skills/dispatch/scripts/fly_machines.py stop --app <app> --machines <m1,m2> --apply
python .claude/skills/dispatch/scripts/fly_machines.py cycle --app <app> --machines <m1,m2> --apply
```
