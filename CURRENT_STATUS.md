# Current status

Updated 22 November 2026. The source project has been simplified to the original Project Brief and Setup Guide. The user authorized five focused local commits under their configured Git identity; no push is authorized. No model inference, 500-ticket rerun or final validation run occurred during cleanup.

## Current application

- Local Python batch command accepts arbitrary input/output paths and saves reports, per-ticket outcomes and decision logs.
- FastAPI remains the programmatic interface, with built-in API documentation and authenticated controls.
- Prometheus and Grafana remain required monitoring. Compose starts only these two services; Python exposes persistent metrics separately on port 8001.
- Required classification, retrieval, routing, cited generation, blocking validation, handoffs, durable logs, kill switch, tests and CI remain.
- Fresh setup uses a free model. Historical paid adapters, cap accounting and unknown-charge reservation safeguards remain for audit compatibility; normal setup never selects paid mode or resets old charges.

Removed: custom React console, Node/Vite/Playwright build and browser tests, static-file serving, UI model catalog and bundled dataset selector, optional Decider experiment, application-container packaging, fixed-500 dataset helper and obsolete probe scripts. Old source and uncommitted changes were preserved before deletion.

## Verification of the simplified project

| Check | Result and limits |
|---|---|
| Fresh Python environment | Python 3.12.13; pinned requirements installed from existing cache into a new temporary venv on macOS arm64 |
| Final remaining Python suite | **391 passed** in 8.03 seconds, including new local-command/API contracts and existing safety, audit, paid-cap, ownership, metrics and failure-handling checks |
| Clean-directory native batch | Seven records processed unattended; seven handoffs, one malformed input, duplicate ticket IDs retained; complete reports and unique outcomes by input position |
| Native batch limitation | Automation stopped and no model key. Actual classifier/retrieval ran; this is safe-handoff/export evidence, not live answer quality |
| API and monitoring HTTP checks | API liveness/readiness/schema, metrics, Prometheus and Grafana returned 200; Prometheus target was up |
| Grafana | Provisioned file hash matched; all 12 queries across 11 panels succeeded; seven persisted outcomes visible to Prometheus. No browser visual review |
| Runtime cleanup | Temporary API/metrics verification processes stopped; legacy app container stopped. Monitoring containers retain their data; start the documented local metrics process to resume scrapes |
| Platforms | This simplified revision was exercised natively on macOS arm64; fresh Linux/Windows execution is not claimed |

Current source hash: `75b881d3410b7c8a174d2d80988a33a10519d76c2556c50d82bd8d312ccdf912`.

## Evidence

- Cleanup summary: `storage/simplification-summary.json`
- Final tests: `storage/simplification-final-tests.log`
- Fresh install: `storage/simplification-fresh-install.log`
- Native reports: `storage/simplification-local-run/`
- HTTP checks: `storage/simplification-local-operations.json`
- Dashboard queries: `storage/simplification-monitoring-queries.json`
- Previous source: `storage/source-before-python-simplification.tar.gz`
- Previous uncommitted patch: `storage/source-before-python-simplification.patch`
- Earlier status/checklist: `storage/current-status-before-python-cleanup.md`, `storage/next-steps-before-python-cleanup.md`

All generated evidence is local and ignored. Existing databases, original datasets, Completed workbooks and legacy Docker audit volume were not deleted or migrated.

## Historical results and outstanding limits

The earlier GPT-4.1 development run processed 500/500 records: 76 answers and 424 escalations, including 40 blocked outputs. It had zero missing/duplicate outcomes. Preserve it as evidence for its own recorded source revision; do not repeat it routinely or present it as evaluation of this simplified build.

Historical target misses remain: answer p95 8.33 seconds, held-out urgency accuracy 41%, missed calibration/retrieval targets, and AI reviewers flagging 15/50 and 14/50 answers for partial support. Prompt mitigations were checked offline, not proven with new live inference. AI reviews do not replace required independent human reviews. Business closure, real customer satisfaction and sustained availability are unproven.

Local `../05_Datasets/validation_tickets.json` is supplied validation, distinct from the unavailable hidden assessor dataset. Final supplied validation has not started. Its old freeze/preflight fingerprints became obsolete after cleanup. The pre-cleanup credit check passed but the paid-cap estimate failed: USD 1.5319200 estimated versus USD 1.52160505 remaining, with USD 1.97260615 known charges and USD 0.0057888 unknown charge held under the USD 3.50 cap. These are historical ledger observations, not a fresh current credit check. No top-up, cap change or unknown-charge release occurred.

The former custom-console screenshot task is obsolete because that console was removed. It must not block the Python deliverable or be reported as completed UI evidence.

## Submission work and order

The user will record the video. Finish source acceptance/evidence first, then the existing Completed workbooks, then the report. Stage 1–4 exist; the required Stage 5 revision log and actual effort log remain outstanding. Preserve personal judgement, reflections and hours; do not invent them. The requested dataset clarification is staged in `.local-notes/docs/validation-dataset-note.txt`; originals were backed up to `../Completed/backups/2026-11-22-validation-note/`. DOCX editing/render verification has not occurred because the required document runtime was unavailable in this session.
