# CloudServe Support

Process support tickets from a JSON file into cited answers or contextual human handoffs. Results and decision logs are saved locally. Delivery is simulated; the application does not send messages to customers.

## 1. Before you start

You need **Python 3.12**, Git, an internet connection and an **OpenRouter API key** with access to a free model. The first run downloads the MiniLM embedding model. Trained classifiers, documentation and sample tickets are included; your own dataset is optional.

The quick start below uses macOS and a bash/zsh terminal. Linux users should use the [Linux installation commands](docs/OPERATIONS_GUIDE.md#linux-installation), then continue at step 3. Docker, Node.js and monitoring services are optional for batch processing.

## 2. Install

```bash
git clone https://github.com/bharathparsam/cloudserve-support.git
cd cloudserve-support
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Keep this terminal open and run the remaining commands from the repository directory. In a new terminal, return to this directory and run `source .venv/bin/activate` again.

## 3. Create your configuration

```bash
python -m scripts.setup_secrets
```

Open the generated `.env` in your editor:

- Set `OPENROUTER_API_KEY` to your own key from [OpenRouter](https://openrouter.ai/settings/keys).
- Set `MODEL_NAME` to an available free model using its exact `provider/model:free` identifier. The template provides a free Gemma candidate.
- Keep `FREE_ONLY=true` and the remaining defaults for your first run.

The setup command generates local secrets and creates storage. If `.env` already exists, keep it and edit it directly. Never commit it.

Save your model selection:

```bash
python -m scripts.configure
```

On an existing installation, stop automation with `python -m scripts.control --stop` and wait for active jobs to finish before configuring. Saved model settings override `.env`; rerun configure after changing your model. See [all environment settings](docs/OPERATIONS_GUIDE.md#environment-settings) for details.

## 4. Run the included sample

```bash
python -m scripts.control --enable --reason "First sample run"
python -m evaluation.harness --input data/samples/four_channels.json --output storage/results/sample-01
```

Enabling checks local assets and provider metadata. The batch then processes four sample tickets, one for each supported channel. Free-model rate limits, unsupported requests or failed checks can produce handoffs; a completed run does not guarantee automatic answers.

For another run, use a new output directory such as `storage/results/sample-02`. If enabling fails, follow [troubleshooting](docs/OPERATIONS_GUIDE.md#troubleshooting).

## 5. Read the results

Open `storage/results/sample-01/metrics.md` for the summary. The same directory contains:

| File | Contents |
|---|---|
| `outcomes.jsonl` | Each ticket's answer or handoff |
| `decisions.jsonl` | Detailed processing decisions |
| `metrics.json` | Machine-readable metrics |
| `manifest.json` | Run status and configuration evidence |

When finished, stop automatic answers:

```bash
python -m scripts.control --stop
```

## Use your own tickets

Supply a JSON array with `channel` and `body` for each ticket; `subject` is optional. Supported channels are `email`, `chat`, `docs_comment` and `forum`.

```json
[
  {"channel": "email", "subject": "Rate limit", "body": "How should I retry a 429 response?"}
]
```

Enable automation again if you stopped it, then run:

```bash
python -m evaluation.harness --input "/path/to/tickets.json" --output storage/results/custom-01
```

## Optional tools

- API: `python -m src.api`, then open http://localhost:8000/docs. Protected endpoints require the `OPERATOR_TOKEN` from your local `.env` as a bearer token.
- Metrics: `python -m scripts.monitor`, then open http://localhost:8001/metrics.
- Prometheus/Grafana: follow the [monitoring setup](docs/OPERATIONS_GUIDE.md#monitoring-on-macos-without-docker).
- Tests: `python -m pytest tests/ -q`.
- Formal assessment runs: follow the [final evaluation protocol](docs/OPERATIONS_GUIDE.md#final-evaluation-protocol).

See the [operations guide](docs/OPERATIONS_GUIDE.md) for advanced configuration and troubleshooting. Code and prompts used Codex assistance; see [architecture](docs/architecture.md) and the [prompt register](prompts/README.md).
