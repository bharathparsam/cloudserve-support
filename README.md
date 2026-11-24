# CloudServe Support

A local Python pipeline that processes ticket JSON files and saves answers, handoffs, decision logs and metrics. Docker is not required.

## 1. Clone

```bash
git clone https://github.com/bharathparsam/cloudserve-support.git
cd cloudserve-support
```

## 2. Install and create `.env`

Python 3.12 on macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m scripts.setup_secrets
```

On Linux, install the CPU PyTorch wheel and the appropriate `requirements-linux-*.txt` lock instead; see [operations](docs/OPERATIONS_GUIDE.md). Keep an existing `.env` rather than regenerating it.

## 3. Configure

Edit `.env`: set `OPENROUTER_API_KEY` and an available free `OPENROUTER_MODEL` (`provider/model:free`). Keep `FREE_ONLY=true`. Never commit credentials.

```bash
python -m scripts.configure
```

## 4. Run your chosen file

```bash
python -m scripts.control --enable --reason "Local batch"
python -m evaluation.harness --input "/path/to/tickets.json" --output storage/results/run-01
```

**You choose the file with `--input`; it is not hardcoded.** Input is a JSON array with `channel` and `body` for each ticket. Supported channels: `email`, `chat`, `docs_comment`, `forum`. A sample is included at `data/samples/four_channels.json`. Use a new output folder for each run.

Free models can be rate-limited; unavailable or unsafe responses become handoffs. For formal evaluation, follow the [evaluation protocol](docs/OPERATIONS_GUIDE.md#final-evaluation-protocol).

## 5. Open the reports

The output folder contains `metrics.md`, `metrics.json`, `outcomes.jsonl`, `decisions.jsonl`, `manifest.json` and detailed JSON/CSV analysis. Unsafe or unsupported tickets receive handoffs.

- API: `python -m src.api` → http://localhost:8000/docs
- Metrics: `python -m scripts.monitor` → http://localhost:8001/metrics
- Stop releases: `python -m scripts.control --stop`
- Tests: `python -m pytest tests/ -q`

## 6. Prometheus and Grafana without Docker

On macOS with [Homebrew](https://brew.sh), install and prepare the local configuration once:

```bash
brew install prometheus grafana
mkdir -p storage/grafana-provisioning
cp -R monitoring/grafana/provisioning/. storage/grafana-provisioning/
sed 's/host.docker.internal/127.0.0.1/' monitoring/prometheus.yml > storage/prometheus-local.yml
sed 's|http://prometheus:9090|http://127.0.0.1:9090|' monitoring/grafana/provisioning/datasources/default.yml > storage/grafana-provisioning/datasources/default.yml
sed "s|/var/lib/grafana/dashboards|$PWD/monitoring/grafana/dashboards|" monitoring/grafana/provisioning/dashboards/default.yml > storage/grafana-provisioning/dashboards/default.yml
```

From the repository folder, run each command in a separate terminal. Activate `.venv` in the Python terminal:

```bash
# Terminal 1: ticket metrics
python -m scripts.monitor

# Terminal 2: Prometheus
prometheus --config.file=storage/prometheus-local.yml --storage.tsdb.path=storage/prometheus --web.listen-address=127.0.0.1:9090

# Terminal 3: Grafana
GF_PATHS_PROVISIONING="$PWD/storage/grafana-provisioning" grafana server \
  --homepath "$(brew --prefix grafana)/share/grafana" \
  cfg:default.paths.data="$PWD/storage/grafana" \
  cfg:default.paths.logs="$PWD/storage/grafana/log" \
  cfg:server.http_addr=127.0.0.1
```

Open [Prometheus](http://localhost:9090) or [Grafana](http://localhost:3000). On first Grafana login, use `admin` / `admin` and change the password. Open the **CloudServe Support** dashboard; its data source is configured automatically. Start monitoring before a batch to see live rates. Press `Ctrl+C` in each terminal to stop monitoring.

Code and prompts used Codex assistance; see [architecture](docs/architecture.md) and the [prompt register](prompts/README.md).
