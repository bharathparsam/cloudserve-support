# CloudServe Support architecture

One FastAPI process serves React static files, operator API, Prometheus metrics and a durable SQLite worker. UI and arbitrary-path CLI share PipelineService and the compiled LangGraph state machine. LangGraph nodes perform allowlisted normalization, TF-IDF/logistic classification, Chroma/MiniLM retrieval, sensitivity assessment, deterministic policy routing, structured drafting, blocking validation and atomic persistence/release. No external messages are sent.

SQLite is authoritative for inputs, stage events, jobs, outcomes, releases, handoffs, cache, reviews and automation control. Stop uses the same write serialization as release. A processing epoch captured at admission must remain unchanged even after stop then re-enable. No draft is returned before release commits. Store failures interrupt/incomplete a run rather than substituting memory logs.

Runtime consumes frozen development-trained models and the supplied corpus. Labels/history/grouping are protected sidecars; provider/model input contains sanitized subject/body only. Generated factual content is claims with retrieved citation references. Every mandatory check must pass. Missing provider key or required assessment/check causes handoff. Fixture providers exist only in isolated tests, and are not live evidence.

Ports are localhost-only. Operator mutations and original-input access use a local bearer token retained in UI memory. No production tenancy/identity guarantee is claimed. Credentials remain in restricted local .env; model keys never travel through the browser.
