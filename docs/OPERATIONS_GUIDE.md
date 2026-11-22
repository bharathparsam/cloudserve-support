# Local operations

Use the README for Python installation, batch processing, FastAPI and monitoring. Ordinary work needs no browser console, Node.js, application container or model picker. Keep the API, CLI and metrics server on the same `STORAGE_PATH` and `DATABASE_URL`. Fresh defaults use `storage/decisions.db`; changing storage selects a different database, not a migration.

## Configuration and control

`python -m scripts.configure --model provider/model:free --timeout 30 --retries 2` saves audited free-model settings while automation is stopped and no run is active. Saving does not call a model or prove provider availability. Saved settings override environment defaults; rerun configure after changing model settings in `.env`. Legacy observer experiments were removed; explicitly reconfigure an old shadow/enforce selection rather than silently accepting it.

`python -m scripts.control --enable --reason "Local processing"` checks assets and provider metadata before enabling answers. Metadata/catalog verification is not proof of inference availability or quota. Provider failures during processing produce handoffs. `python -m scripts.control --stop --reason "Incident"` changes the durable control epoch immediately without waiting for the processing lock. Old in-flight drafts cannot release after stop/re-enable. The API exposes the same controls. API shutdown also stops releases.

The CLI owns its batch jobs and uses a separate native Chroma index built from the same frozen corpus/embeddings. Run one processing command at a time. If interrupted, preserve its output and manifest; do not relabel a retry as the original unattended run. Never edit live database files or delete storage to clear an error.

## Input and reporting

The ordinary command is `python -m evaluation.harness --input INPUT.json --output OUTPUT_DIRECTORY`. Its default kind is `batch`, not a claim that the input is development or final validation. It handles arbitrary paths and counts. Labels/history remain evaluation sidecars, not classifier or provider inputs. Missing labels make label-dependent metrics unavailable. An output directory must be new or empty; previous evidence is never overwritten.

The supplied local validation file is distinct from the unavailable hidden assessor file. Preserve the completed 500-ticket development evidence for its recorded revision. Do not repeat that batch routinely or claim it proves the simplified revision. Supplied-validation contents remain excluded from development tuning. The original pack contains conflicting validation-use instructions; follow the authoritative Project Brief's final-only use and report actual runs honestly. Dataset counts in prose conflict; the harness must process the actual file length.

## Final evaluation protocol

Final evaluation remains a deliberate separate step, after changes and any agreed evidence are complete. Freeze exact code/assets/prompts/settings/protocol first. Use the original development input only to define the frozen short/long boundary:

```bash
python -m scripts.freeze --development-input PATH_TO_DEVELOPMENT.json --output storage/freeze-new.json
```

Preserve any old `storage/freeze.json` before activating the new manifest at that canonical path. Never overwrite an old freeze or preflight without preserving it. Then create a new preflight, using a record upper bound that covers the expected input without inspecting validation for development:

```bash
python -m scripts.preflight --records 120 --scope supplied_validation --output storage/preflight-new.json
```

Review the result. A verified free model listing does not prove enough free-request quota. Do not bypass a failed quota, asset or budget gate. Preserve the old preflight before activating a successful new candidate as `storage/preflight.json`. Preflight expires after one hour. With automation explicitly enabled, the frozen final command is:

```bash
python -m evaluation.harness --input PATH_TO_VALIDATION.json --output storage/evaluation/final-01 --kind supplied_validation
```

Reconcile every position to an answer or escalation; disclose target misses and external limitations. Do not tune on supplied validation or present a repeat as untouched performance. The hidden assessor dataset is not supplied; never claim to have run it.

## Historical paid evidence

The original brief requires free-tier or local inference. Earlier user-authorized paid development/evaluation is a disclosed deviation. The retained paid adapters and audit/ledger utilities protect historical charges and unknown-charge reservations; no normal setup command enables paid mode. The USD 3.50 authorized cap is durable, shared and cannot be reset by changing models or configuration. Unknown timeout charges remain fully reserved. Never delete the ledger, release an unknown charge, increase the cap or initiate paid calls automatically.

An already authorized paid runtime still requires explicit `--authorize-paid-evaluation`, scoped authorization, matching freeze and fresh smoke/budget/credit preflight. Cost estimates are not guarantees. The latest pre-simplification preflight failed its cap check; its code/configuration fingerprints are now obsolete. Preserve it as historical evidence. No new paid or final validation run is authorized by this cleanup.

## Reviews and evidence

`evaluation.export` is a library used by API jobs; `export_run(settings, store, pipeline, run_id, output_path)` exports existing persisted decisions without model calls. Review sheets are left blank for actual independent human reviewers. `python -m evaluation.review --help` describes review import. AI reviews are not human reviews; no software fills personal reflections, hours or approvals.

## Monitoring and incident response

Keep `python -m scripts.monitor --host 0.0.0.0` running so Prometheus can scrape `host.docker.internal:8001`. Compose starts only Prometheus and Grafana; their ports bind to localhost. The metrics server serves numeric metrics only, not original tickets or keys. For an incident: stop automation, preserve the affected run/logs, assess private-data or unsupported-answer exposure, notify the accountable owner, remediate and test the cause, then explicitly re-enable. Record the actual incident and accountable person; do not invent approvals.

To back up consistently, stop automation and all writers first, then copy the database and its WAL/SHM files together, plus prompts, policies and run artifacts. Preserve any legacy Docker `audit_data` volume; this cleanup does not migrate or delete it. Do not point native and container processes at the same writable Chroma directory.

## Troubleshooting

- Missing imports: activate `.venv` and install the documented platform lock.
- First startup is slow: MiniLM downloads once; later starts use the cache.
- Provider or model unavailable: verify local credentials/model selection; keep handoffs and report the outage.
- Configuration refused: stop automation and wait for active runs; an invalid saved configuration must be explicitly corrected.
- No Grafana data: keep the metrics process alive, check Prometheus targets, and allow at least one 15-second scrape. Historical cumulative metrics do not prove sustained service availability.
- Missing report or nonzero exit: preserve the manifest and log; audit/export failure means incomplete evidence rather than success.
