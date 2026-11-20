# Operations guide

Run commands from the application root. See [README](../README.md) for installation and startup, [architecture](architecture.md) for the workflow, and [OpenAPI](openapi.json) for the API reference.

## Configuration and access

`python3 scripts/setup_secrets.py` creates a restricted `.env` from `.env.example`, generates the local operator/Grafana credentials and refuses to overwrite existing configuration. Keys never travel through the browser. After editing `.env`, recreate the app with `docker compose up -d --force-recreate app`.

Operator mutations and original ticket access require the bearer token. The console holds it only in page memory. Ports bind to localhost. This application does not implement multi-tenant production identity or send customer messages.

Saved model settings override environment defaults and persist in SQLite. Saving requires stopped automation, an idle batch and verified free pricing/capability. Decider can be disabled or run in shadow mode; enforced release requires reviewed calibration. Catalog verification establishes prices and capabilities, not inference availability or sufficient quota.

## Tickets and batches

The four accepted channels are `email`, `chat`, `docs_comment` and `forum`. JSON imports are arrays with a nonblank `body`; `subject` and `ticket_id` are optional. Download the exact template in Runs & evaluation. Malformed individual records become explicit handoffs; duplicate external IDs retain separate input positions.

The bundled selector uses only the fixed, fingerprinted development file in `data/input/development_tickets.json`. Start with the first five records. Do not upload supplied validation as development data. Closing the browser does not cancel a run. Stop blocks new answer releases; remaining batch records receive handoffs.

For a separately labelled development run, mount the chosen file in `data/input/`, then use a fresh output directory:

```bash
docker compose exec -T app python -m evaluation.harness --input /data/input/development-demo.json --output /app/storage/evaluation/dev-01 --kind development
```

## Evaluation

Training, calibration and retrieval design use development data only. Supplied validation must remain unopened until the final gate. Before that run, finish development, freeze source/assets/protocol and verify enough free requests for the complete input. Monetary account balance is not request quota. Existing snapshots must be preserved; use new paths for revisions.

```bash
docker compose exec -T app python -m scripts.freeze --development-input /data/input/development_tickets.json
docker compose exec -T app python -m scripts.preflight --records 80
docker compose exec -T app python -m evaluation.harness --input /data/input/validation_tickets.json --output /app/storage/evaluation/final-01 --kind supplied_validation
```

These commands enforce the current freeze, model, quota and operator controls. The final gate blocks when prerequisites fail. Development-only paid authorization cannot pass the free-only supplied-validation gate. Human reviewer identities, scores, closure and approvals must come from real people; blank metrics remain unavailable.

## Explicit paid development test

Normal runtime stays free-only. A separately authorized development test permits one selected ticket through the same workflow with GPT-4.1 Mini or GPT-4o Mini. Its adapter verifies price ceilings, reserves a conservative cost bound, allows three requests with no retries, and caps the entire test at USD0.25. Tools, web search and paid fallback are disabled. Each additional test budget requires explicit authorization.

```bash
docker compose exec -T app python -m scripts.paid_answer_smoke --authorize-paid-test --model openai/gpt-4.1-mini --budget-usd 0.25 --output /app/storage/live-answer/new-paid-test
```

Use a new output path and one adapter for the authorized budget. This creates a real local `paid_development_test` run, logs actual costs, leaves saved normal model settings unchanged and restores a previously stopped automation state. Do not count this as free inference or independent quality evaluation.

## Troubleshooting and backups

- Check `docker compose ps`, `/health/ready` and `docker compose logs app` for readiness and safe operational errors.
- HTTP429 means the model provider is rate limited. A preflight or changed key does not guarantee inference availability. No paid fallback is attempted.
- Low intent confidence, missing source support or unknown/failed checks produce a handoff. Inspect routing and stage evidence; a completed batch is not necessarily an answered batch.
- Stop automation before maintenance. Stop the app before copying `storage/`, or use a consistent SQLite backup that includes WAL state. Preserve frozen assets and monitoring volumes separately.
- `docker compose stop` retains data. Do not delete volumes or reset audit records to improve results. Interrupted jobs are recorded explicitly.
- Grafana's environment password initializes a fresh database; changing `.env` does not rotate an existing Grafana account.

## Verification and limits

```bash
docker compose run --rm --no-deps app python -m pytest tests/ -q
```

For frontend build/browser checks, follow the README. CI uses synthetic providers without live keys or validation; those passes do not establish live model quality. Linux arm64 has been exercised locally; the amd64 dependency lock is present but its image still needs verification.

Known development limitations include urgency accuracy of41%, restrictive retrieval coverage, repeated templates across training/calibration, and weak generalization to novel short questions. Decider probabilities are not CloudServe-calibrated. One authorized paid development example passed all11 release checks at reported costUSD0.0023492, taking15.216s; that does not establish the3s latency or general quality targets. Independent reviews and final supplied validation remain outstanding.

Generated evidence and internal notes are preserved locally but ignored by Git. Reports, video and original workbooks remain separate human-owned submission work.
