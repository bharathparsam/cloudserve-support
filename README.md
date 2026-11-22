# CloudServe Support

A local Python pipeline that reads support tickets, retrieves CloudServe documentation, and saves cited answers or human handoffs with decision logs and a metrics report. FastAPI provides the programmatic interface. Prometheus and Grafana provide monitoring; there is no custom web application.

## Set up

Use Python 3.12 and run commands from this directory. Initial embedding downloads and model inference need internet access. Docker Compose is needed only for Prometheus and Grafana.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m scripts.setup_secrets
```

The pinned `requirements.txt` is for macOS. On Linux, replace its installation command with these commands, choosing `requirements-linux-arm64.txt` for ARM machines:

```bash
python -m pip install --no-deps --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu
python -m pip install -r requirements-linux-amd64.txt
```

Edit your local `.env` and set `OPENROUTER_API_KEY`. Set `OPENROUTER_MODEL` to an available explicit `provider/model:free` identifier. The template contains an initial candidate, not a guarantee of current availability or free quota. Keep credentials out of Git. The setup command generates the API operator token and Grafana password without printing them and never overwrites an existing `.env`.

Save the model configuration locally:

```bash
python -m scripts.configure
```

Trained classifier assets, the documentation corpus, and index artifacts are included under `assets/`; ordinary setup does not retrain them. The embedding model downloads on first use and is then cached.

## Run a file and save the report

The input is a JSON array. Each ticket has `channel` (`email`, `chat`, `docs_comment`, or `forum`), `body`, and optional `subject` and `ticket_id`. See `data/samples/four_channels.json`.

Explicitly enable automatic answers, then run the input file:

```bash
python -m scripts.control --enable --reason "Local batch processing"
python -m evaluation.harness --input data/samples/four_channels.json --output storage/evaluation/run-01
```

Use any input path, filename and record count. Choose a new or empty output directory for each run. The command processes the whole array unattended and exits nonzero if it cannot complete durably. It produces:

| File | Contents |
|---|---|
| `metrics.md`, `metrics.json` | Computed volume, business, technical and governance measures, with unavailable measures identified |
| `outcomes.jsonl` | One answer or escalation per input position |
| `decisions.jsonl` | Processing decisions, sources, checks and version traceability |
| `manifest.json` | Run identity, input/source fingerprints, state and reconciliation |
| Other JSON/CSV files | Classification, retrieval and group analysis; blank sheets for independent human reviews |

Without enabled automation, records receive safe handoffs. Missing documentation, unsafe input, unavailable models and failed checks also produce handoffs. An automatic answer is not proof of business closure or customer satisfaction. A completed batch with only handoffs demonstrates continuity, not answer quality.

Stop automatic answers immediately, including releases from tickets already in flight:

```bash
python -m scripts.control --stop --reason "Operator stop"
```

Stop does not cancel a provider request already sent. The batch continues with handoffs. Run `python -m scripts.control` to inspect control state. All commands share the same local database; use consistent path settings across terminals.

## Monitor the run

Keep this metrics process running in another activated terminal:

```bash
python -m scripts.monitor --host 0.0.0.0
```

It serves numeric metrics at port 8001 from the persistent decision store, including after a batch exits. It does not run models or submit tickets. Start monitoring services:

```bash
docker compose up -d
```

- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 — user `admin`, password from your local `.env`.

The provisioned CloudServe dashboard shows ticket throughput by channel/outcome, automatic answers versus escalations, median/p95 latency, guardrail activations and confidence distribution. Prometheus scrapes the host metrics server every 15 seconds; allow one scrape before checking results. Stop services with `docker compose stop`; data is retained.

## Use the FastAPI interface

```bash
python -m src.api
```

API documentation: http://localhost:8000/docs. Submit tickets, inspect outcomes/sources/logs and operate the stop control through this interface. Mutations and original-input access require `Authorization: Bearer <OPERATOR_TOKEN>`. The batch command works without the API server. API and CLI use the same processing pipeline and database; API workers cannot take over CLI batches.

## Test

```bash
python -m pytest tests/ -q
```

Tests isolate storage and credentials and use synthetic model responses. They do not consume model quota or establish live model quality. GitHub Actions runs the Python suite on push and pull requests.

See [operations](docs/OPERATIONS_GUIDE.md) for final evaluation, historical paid-run safeguards, review import and backups, and [architecture](docs/architecture.md) for the component and governance design. Original project requirements call for free models; historical paid development was an explicitly authorized deviation, not evidence of compliance or assessor approval. Model unavailability must remain visible and degrade safely.

Code and prompt development used Codex assistance. Libraries, the supplied CloudServe documentation corpus, and model artifacts are identified in their manifests and prompt register. Report, workbooks, human reviews and video are separate submission deliverables.
