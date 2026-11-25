# CloudServe Support

A local support workspace for processing ticket datasets into checked, cited answers or contextual human handoffs. Upload your JSON file, follow the processing flow, inspect decisions and download reports. Delivery is simulated; no customer messages are sent.

## 1. Requirements

- **Python 3.12**, Git and a browser.
- An internet connection for installation and the first MiniLM model download.
- Your own **OpenRouter API key** with access to the selected model.
- **Docker with Compose** for the monitoring setup below. [Native macOS monitoring](docs/OPERATIONS_GUIDE.md#monitoring-on-macos-without-docker) is also supported.

The backend serves the frontend. **No Node.js, frontend build or separate frontend server is needed.** The required classifier artifacts and documentation corpus are included; user datasets and recording samples are not.

These commands use macOS with bash/zsh. Verify `python3.12 --version` first; install Python 3.12 if the command is unavailable. Linux users should follow [Linux installation](docs/OPERATIONS_GUIDE.md#linux-installation), then continue at step 3. Windows setup is not covered.

## 2. Clone and install

```bash
git clone https://github.com/bharathparsam/cloudserve-support.git
cd cloudserve-support
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

Run all remaining commands from **`cloudserve-support/`**. Activate `.venv` in every new Python terminal.

## 3. Configure your local environment

The repository includes **`.env.example`** with non-secret defaults. Generate your local configuration:

```bash
python -m scripts.setup_secrets
```

This creates `.env`, generates `OPERATOR_TOKEN` and `GRAFANA_ADMIN_PASSWORD`, and creates storage. If `.env` already exists, keep it and edit it directly.

Open `.env` in your editor, set `OPENROUTER_API_KEY` to your [OpenRouter key](https://openrouter.ai/settings/keys), and keep `FREE_ONLY=true` and the other defaults for your first run. Then save the initial free-model selection:

```bash
python -m scripts.configure
```

For an existing installation, stop automation with `python -m scripts.control --stop` and wait for active jobs before configuring. Saved model settings override `.env`; editing the file alone does not replace them. Keep `.env` private and out of Git. See [environment settings](docs/OPERATIONS_GUIDE.md#environment-settings) for advanced options.

## 4. Start the backend and open the frontend

**Application terminal:**

```bash
source .venv/bin/activate
python -m src.api
```

Keep it running and open **http://localhost:8000** for the frontend dashboard. API documentation is at http://localhost:8000/docs. To use another port, run `python -m src.api --port 8002` and open http://localhost:8002.

1. Click **Connect** and enter `OPERATOR_TOKEN` from your local `.env`. Reconnect after reloading the page.
2. Open **Settings**, choose a model and click **Save model**. Model changes require an idle batch and stop automatic answers.
3. Choose **your own JSON dataset** and click **Run dataset**. The app checks the provider before enabling answers and starting the batch.
4. Follow the processing stages. Click **Inspect** on a ticket to see its outcome, sources and guardrail checks.
5. Open **Run history** for previous runs. **Stop answers** prevents further automatic releases; remaining tickets receive handoffs.

Free model choices are **Gemma 4 26B** and **Llama 3.1 8B `:free`**. Paid choices are **GPT-4.1** and **GPT-4.1 Mini**, with a shared budget cap and explicit confirmation for each run. A listed model does not guarantee working inference, quota or successful answers. Free mode has no paid fallback.

Closing the browser does not stop a batch; keep the application terminal running. Press **Ctrl+C** there to shut down. Restart the backend and refresh the browser after code changes.

## 5. Input format and reports

Your dataset must be a JSON array. Each ticket needs a supported `channel` and a nonempty `body`; `subject` is optional. Supported channels: `email`, `chat`, `docs_comment`, `forum`.

```json
[
  {"channel": "email", "subject": "Rate limit", "body": "How should I retry a 429 response?"}
]
```

Use **Report ZIP** for the selected run, **Progress ZIP** for a timestamped in-progress snapshot, or **Download all reports** in Run history. Report generation makes no model calls and preserves earlier exports.

| Local path | Contents |
|---|---|
| `storage/decisions.db` | Runs, ticket decisions, controls and saved model selection |
| `storage/evaluation/<run-id>/` | Dashboard-created run exports |
| `storage/reports/<run-id>/<snapshot-hash>/` | Report files and `report.zip` |
| `storage/reports/downloads/` | Combined history ZIPs, including an index of export errors |

Datasets, secrets, reports, caches and databases stay local and are ignored by Git. The documentation corpus and classifier assets under `assets/` are required runtime files.

## 6. Start Prometheus and Grafana

Keep the application terminal running. Start Docker, then use two more terminals from the project directory.

**Metrics terminal:**

```bash
source .venv/bin/activate
python -m scripts.monitor --host 0.0.0.0
```

**Monitoring terminal:**

```bash
docker compose up -d
```

| Dashboard | URL |
|---|---|
| **Grafana: CloudServe Support** | **http://localhost:3000/d/cloudserve/cloudserve-support** |
| Prometheus targets | http://localhost:9090/targets |
| Prometheus alerts | http://localhost:9090/alerts |
| Prometheus rules | http://localhost:9090/rules |
| Exported numeric metrics | http://localhost:8001/metrics |

For a new Docker Grafana installation, use username **`admin`** and `GRAFANA_ADMIN_PASSWORD` from your local `.env`. Existing Grafana installations retain their credentials. Dashboard and datasource setup is automatic. The application also links to Grafana and Prometheus.

Check that Prometheus shows `cloudserve` and `prometheus` as **UP**. Grafana shows stored outcomes, channel traffic, latency, guardrails and provider observations. Allow roughly 30–60 seconds for initial scrapes and rules. Recent-rate and latency panels need **new processing observed between scrapes**; historical totals alone do not populate them. A reachable metrics endpoint is not proof of live model availability. Alerts are displayed locally; external notifications are not configured.

If metrics is already available at http://localhost:8001/metrics, do not start a second metrics process. Use either Docker-based monitoring or the [native setup](docs/OPERATIONS_GUIDE.md#monitoring-on-macos-without-docker), not both on the same ports.

To stop monitoring without deleting its data:

```bash
docker compose stop
```

Press **Ctrl+C** in the metrics terminal. Full dashboard descriptions and native setup are in the [operations guide](docs/OPERATIONS_GUIDE.md).

## 7. Optional command-line processing and tests

With `.venv` active and a free model selected:

```bash
python -m scripts.control --enable --reason "Local processing"
python -m evaluation.harness --input "/absolute/path/to/your-tickets.json" --output storage/results/run-01
python -m scripts.control --stop
```

Supply your own input path and a new output directory for each run. Do not run a CLI batch concurrently with a dashboard batch. CLI reports remain in the chosen output directory, and the run also appears in dashboard history.

Run the automated tests with:

```bash
python -m pytest tests/ -q
```

## Troubleshooting

| Problem | Next step |
|---|---|
| Frontend does not open | Keep `python -m src.api` running and use its port. No frontend build is needed. |
| Connection rejected | Use `OPERATOR_TOKEN`, not the OpenRouter key. Reconnect after reloading. |
| Model cannot save or run | Check key/model access, quota and budget. Wait for any active batch to finish. |
| Tickets all become handoffs | Inspect their reasons: stopped automation, low confidence, insufficient evidence and provider failure are different outcomes. |
| Paid authorization error during an ordinary CLI run | Stop automation and select a free model in Settings or rerun `python -m scripts.configure`. |
| Port 8001 is occupied | Check `/metrics`; a metrics service may already be running. |
| Grafana has no data | Keep metrics running, check Prometheus targets, and allow the scrape window. |
| Output directory exists | Choose a new directory; existing reports are not overwritten. |

Formal assessment runs follow the separate [evaluation protocol](docs/OPERATIONS_GUIDE.md#final-evaluation-protocol). Code and prompts used Codex assistance; see [architecture](docs/architecture.md) and the [prompt register](prompts/README.md).
