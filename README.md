# CloudServe Support

Support teams receive repetitive requests across email, chat, documentation comments and forums. CloudServe classifies tickets, retrieves documentation and produces a checked, cited answer or a human handoff.

## 1. Requirements

- Docker with Docker Compose v2.
- Python 3 on macOS/Linux for local credential setup.
- An OpenRouter API key and internet access for initial downloads and inference.

Run these commands from `cloudserve-support/`.

## 2. Create your local configuration

```bash
python3 scripts/setup_secrets.py
mkdir -p data/input
```

The setup script creates `.env` from `.env.example`, generates a secure `OPERATOR_TOKEN` and `GRAFANA_ADMIN_PASSWORD`, and sets your local UID/GID. It does not display secrets or overwrite an existing `.env`.

Open `.env` locally and set `OPENROUTER_API_KEY`. Keep the generated operator token and Grafana password there. **Never commit `.env` or put real credentials in `.env.example`.**

| Setting | Purpose |
|---|---|
| `OPERATOR_TOKEN` | Connect the operator in the application |
| `GRAFANA_ADMIN_PASSWORD` | Sign in to Grafana as `admin` |
| `OPENROUTER_API_KEY` | Authenticate inference requests |
| `OPENROUTER_MODEL` | Initial free generation model |
| `DECIDER_MODEL`, `DECIDER_MODE` | Optional observer; `shadow` or `disabled` for ordinary testing |
| `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES` | Request limits; defaults: 30 seconds and 2 retries |

Model settings can be saved in the UI; saved settings survive restarts and override environment defaults. Normal operation remains free-only. Paid development tests require explicit authorization and a separate capped test command; see the [operations guide](docs/OPERATIONS_GUIDE.md).

## 3. Add development tickets (optional)

If you have the supplied dataset pack next to this directory:

```bash
cp ../05_Datasets/development_tickets.json data/input/
```

This enables the UI's bundled 500-ticket selector. You can also upload your own JSON using the downloadable template. Keep supplied validation separate from development runs.

## 4. Start the application

```bash
docker compose up --build -d
docker compose ps
```

First startup may take several minutes to download and load the embedding model.

| Service | Address | Login |
|---|---|---|
| Support console | http://localhost:8000 | Connect using `OPERATOR_TOKEN` from `.env` |
| Prometheus | http://localhost:9090 | Local access |
| Grafana | http://localhost:3000 | `admin` and `GRAFANA_ADMIN_PASSWORD` from `.env` |

## 5. Process tickets

1. Connect the operator and review **Settings** while automatic answers are stopped.
2. Explicitly enable automatic answers.
3. Use **New ticket**, or **Runs & evaluation** to select the first five development tickets or upload JSON.
4. Inspect the answer or handoff, sources and blocking checks in **Ticket queue**.

Provider failures, low confidence and insufficient documentation produce handoffs. **Stop automatic answers** blocks releases; remaining batch records still receive handoffs.

## 6. Check and stop

Run backend tests:

```bash
docker compose run --rm --no-deps app python -m pytest tests/ -q
```

Optional frontend checks require Node.js 22 and Playwright Chromium:

```bash
cd frontend
npm ci
npx playwright install chromium
npm run build
npm run test:browser
```

From the application root, stop services without deleting stored tickets:

```bash
docker compose stop
```

After changing `.env`, reload the application environment:

```bash
docker compose up -d --force-recreate app
```

For troubleshooting, backups, evaluation and evidence, see the [detailed operations guide](docs/OPERATIONS_GUIDE.md). Reports, video, workbooks and independent human reviews are separate submission tasks.
