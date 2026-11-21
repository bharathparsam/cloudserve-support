import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Activity,
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  Cloud,
  Copy,
  FileJson,
  HandHelping,
  Inbox,
  KeyRound,
  LayoutDashboard,
  Mail,
  MessageSquare,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  Upload,
  X,
} from "lucide-react";
import type { Outcome, SourcePassage, Dimension } from "./contracts";
import type { TicketInput } from "./ticket-input";
type Page =
  | "Overview"
  | "New ticket"
  | "Ticket queue"
  | "Handoffs"
  | "Runs & evaluation"
  | "Settings";
type Obj = Record<string, any>;
type Ticket = {
  record_id: string;
  ticket?: { subject?: string; body?: string; channel?: string };
  outcome?: Outcome | null;
  events?: Obj[];
  status?: string;
  review?: Obj;
  created_at?: string;
};
const channels = [
  { id: "email", name: "Email", icon: Mail },
  { id: "chat", name: "Live chat", icon: MessageSquare },
  { id: "docs_comment", name: "Documentation", icon: BookOpen },
  { id: "forum", name: "Community forum", icon: Inbox },
] as const;
const examples: Record<string, { subject: string; body: string }> = {
  email: {
    subject: "API rate limit guidance",
    body: "How should I handle an HTTP 429 response and retry an API request?",
  },
  chat: {
    subject: "",
    body: "How do I get started with a new CloudServe project?",
  },
  docs_comment: {
    subject: "Webhook configuration",
    body: "How can I configure a webhook and verify its signature?",
  },
  forum: {
    subject: "Billing dispute",
    body: "Please refund the charges on our latest invoice.",
  },
};
const human = (v?: string | null) =>
  v ? v.replaceAll("_", " ") : "Unavailable";
const pct = (v?: number | null) =>
  v == null ? "Unavailable" : `${(v * 100).toFixed(1)}%`;
const seconds = (v?: number | null) =>
  v == null ? "Unavailable" : `${v.toFixed(2)} s`;
function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox aria-hidden />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export default function App() {
  const [page, setPage] = useState<Page>("Overview"),
    [token, setToken] = useState(""),
    [showToken, setShowToken] = useState(false),
    [status, setStatus] = useState<Obj | null>(null),
    [health, setHealth] = useState<Obj | null>(null),
    [control, setControl] = useState<Obj | null>(null),
    [tickets, setTickets] = useState<Ticket[]>([]),
    [runs, setRuns] = useState<Obj[]>([]),
    [ticketTotal, setTicketTotal] = useState(0),
    [offset, setOffset] = useState(0),
    [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<Ticket | null>(null),
    [runId, setRunId] = useState<string | null>(null),
    [runDetail, setRunDetail] = useState<Obj | null>(null),
    [runMetrics, setRunMetrics] = useState<Obj | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [stale, setStale] = useState(false),
    [lastSync, setLastSync] = useState<Date | null>(null),
    [busy, setBusy] = useState(false),
    [source, setSource] = useState<SourcePassage | null>(null),
    [original, setOriginal] = useState<Obj | null>(null),
    [controlOpen, setControlOpen] = useState(false),
    [reason, setReason] = useState(""),
    [search, setSearch] = useState(""),
    [channelFilter, setChannelFilter] = useState(""),
    [actionFilter, setActionFilter] = useState(""),
    [urgencyFilter, setUrgencyFilter] = useState(""),
    [blockedFilter, setBlockedFilter] = useState(false),
    [channel, setChannel] = useState<TicketInput["channel"]>("email"),
    [subject, setSubject] = useState(""),
    [body, setBody] = useState(""),
    [customerName, setCustomerName] = useState(""),
    [customerId, setCustomerId] = useState(""),
    [formError, setFormError] = useState(""),
    [file, setFile] = useState<File | null>(null),
    [reviewNotes, setReviewNotes] = useState(""),
    [reviewStatus, setReviewStatus] = useState("reviewed");
  const dialog = useRef<HTMLDialogElement>(null),
    sourceDialog = useRef<HTMLDialogElement>(null),
    originalDialog = useRef<HTMLDialogElement>(null),
    originalRequest = useRef(0),
    heading = useRef<HTMLHeadingElement>(null);
  const api = useCallback(
    async (path: string, options: RequestInit = {}) => {
      const headers = new Headers(options.headers);
      if (token) headers.set("Authorization", `Bearer ${token}`);
      if (options.body && !(options.body instanceof FormData))
        headers.set("Content-Type", "application/json");
      const res = await fetch(`/api/v1${path}`, { ...options, headers });
      if (!res.ok) {
        let msg = `Request failed (${res.status})`;
        try {
          const json = await res.json();
          msg = json.error?.message ?? msg;
        } catch {}
        throw Error(msg);
      }
      return res.json();
    },
    [token],
  );
  const refresh = useCallback(async () => {
    try {
      const params = new URLSearchParams({
        limit: "25",
        offset: String(offset),
      });
      if (search) params.set("search", search);
      if (channelFilter) params.set("channel", channelFilter);
      if (actionFilter || page === "Handoffs")
        params.set("action", page === "Handoffs" ? "escalate" : actionFilter);
      if (urgencyFilter) params.set("urgency", urgencyFilter);
      if (blockedFilter) params.set("blocked", "true");
      const [s, c, t, r, h] = await Promise.all([
        api("/status"),
        api("/controls/automation"),
        api(`/tickets?${params}`),
        api("/runs?limit=100"),
        fetch("/health/ready")
          .then((r) => r.json())
          .catch(() => null),
      ]);
      setStatus(s);
      setHealth(h);
      setControl(c);
      setTickets(t.items ?? []);
      setTicketTotal(t.total ?? 0);
      setRuns(r.items ?? []);
      if (selected) setDetail(await api(`/tickets/${selected}`));
      if (runId) {
        const [rd, rm] = await Promise.all([
          api(`/runs/${runId}`),
          api(`/runs/${runId}/metrics`),
        ]);
        setRunDetail(rd);
        setRunMetrics(rm);
      }
      setStale(false);
      setLastSync(new Date());
    } catch (e) {
      setStale(true);
      setError((e as Error).message);
    }
  }, [
    api,
    offset,
    search,
    channelFilter,
    actionFilter,
    urgencyFilter,
    blockedFilter,
    page,
    selected,
    runId,
  ]);
  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 1800);
    return () => clearInterval(t);
  }, [refresh]);
  useEffect(() => {
    if (controlOpen || showToken) dialog.current?.showModal();
    else dialog.current?.close();
  }, [controlOpen, showToken]);
  useEffect(() => {
    if (source) sourceDialog.current?.showModal();
    else sourceDialog.current?.close();
  }, [source]);
  function navigate(p: Page) {
    setPage(p);
    setSelected(null);
    setDetail(null);
    setOffset(0);
    window.location.hash = p.toLowerCase().replaceAll(" ", "-");
    setTimeout(() => heading.current?.focus(), 0);
  }
  async function mutate(action: () => Promise<any>, success: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      setNotice(success);
      await refresh();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!body.trim()) {
      setFormError("Enter the customer’s request.");
      return;
    }
    setFormError("");
    if (!token) {
      setShowToken(true);
      return;
    }
    setBusy(true);
    try {
      const result = await api("/tickets", {
        method: "POST",
        body: JSON.stringify({
          channel,
          subject,
          body,
          customer_name: customerName || null,
          customer_id: customerId || null,
        }),
      });
      setPage("Ticket queue");
      setSelected(result.record_id);
      setDetail(null);
      setNotice("Ticket queued. Processing continues if you close this page.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) {
      setError("Choose a JSON array file.");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setError("File exceeds 10 MiB.");
      return;
    }
    if (!token) {
      setShowToken(true);
      return;
    }
    try {
      const parsed = JSON.parse(await file.text());
      if (!Array.isArray(parsed) || parsed.length > 10000)
        throw Error("Use a JSON array containing at most 10,000 records.");
      const data = new FormData();
      data.append("file", file);
      setBusy(true);
      const result = await api("/runs", { method: "POST", body: data });
      setRunId(result.run_id);
      setNotice(
        `Run queued with ${parsed.length} input positions. The server owns execution.`,
      );
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNotice("Copied safe output.");
    } catch {
      setError("Clipboard unavailable. Select and copy the visible text.");
    }
  }
  async function exportArtifact(name: string) {
    try {
      const res = await fetch(`/api/v1/runs/${runId}/artifacts/${name}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok)
        throw Error(
          "Artifact unavailable. Use the documented harness to export this run.",
        );
      const url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const visibleOutcomes = tickets.flatMap((t) =>
      t.outcome ? [t.outcome] : [],
    ),
    answered = visibleOutcomes.filter(
      (o) => o.action === "auto_respond",
    ).length,
    escalated = visibleOutcomes.filter((o) => o.action === "escalate").length,
    blocked = visibleOutcomes.filter((o) => o.blocked).length;
  const nav = [
    { label: "Overview" as Page, icon: LayoutDashboard },
    { label: "New ticket" as Page, icon: Plus },
    { label: "Ticket queue" as Page, icon: Inbox },
    { label: "Handoffs" as Page, icon: HandHelping },
    { label: "Runs & evaluation" as Page, icon: FileJson },
    { label: "Settings" as Page, icon: Settings2 },
  ];
  useEffect(() => {
    if (original) originalDialog.current?.showModal();
    else originalDialog.current?.close();
  }, [original]);
  function clearOriginal() {
    originalRequest.current++;
    setOriginal(null);
  }
  useEffect(() => {
    clearOriginal();
  }, [selected, token]);
  async function showOriginal() {
    const requestNumber = ++originalRequest.current;
    setOriginal(null);
    if (!token) {
      setError(
        "Connect a local operator before viewing protected original input.",
      );
      return;
    }
    try {
      const value = await api(`/tickets/${selected}/original`);
      if (requestNumber === originalRequest.current) setOriginal(value);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const outcome = detail?.outcome;
  function sources(items: SourcePassage[]) {
    return (
      <div className="sources">
        {items.map((s) => (
          <button
            className="source"
            key={s.chunk_id}
            onClick={async () => {
              try {
                setSource(
                  await api(`/sources/${encodeURIComponent(s.chunk_id)}`),
                );
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            <BookOpen size={17} aria-hidden />
            <span>
              <strong>{s.title}</strong>
              <small>
                {s.doc_id} · similarity {s.score.toFixed(3)}
              </small>
            </span>
            <ChevronRight size={16} aria-hidden />
          </button>
        ))}
      </div>
    );
  }
  function dimension(d?: Dimension | null) {
    return d ? (
      <>
        <strong>{human(d.label)}</strong>
        <div className="probabilities">
          <span>
            Raw <b>{pct(d.raw_probability)}</b>
          </span>
          <span>
            Calibrated <b>{pct(d.calibrated_probability)}</b>
          </span>
        </div>
        <details>
          <summary>Class probabilities & alternatives</summary>
          <pre>{JSON.stringify(d.raw_probabilities, null, 2)}</pre>
        </details>
      </>
    ) : (
      <span className="muted">Classification unavailable</span>
    );
  }
  return (
    <div className="app-shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#overview"
          onClick={() => navigate("Overview")}
        >
          <span className="brand-icon">
            <Cloud size={23} aria-hidden />
          </span>
          <span>
            CloudServe<small>SUPPORT CONSOLE</small>
          </span>
        </a>
        <div className="workspace-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {nav.map((n) => (
            <button
              key={n.label}
              aria-current={page === n.label ? "page" : undefined}
              className={page === n.label ? "active" : ""}
              onClick={() => navigate(n.label)}
            >
              <n.icon size={19} aria-hidden />
              {n.label}
              {n.label === "Ticket queue" && ticketTotal > 0 && (
                <span className="nav-count">{ticketTotal}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <ShieldCheck size={20} aria-hidden />
          <div>
            <strong>Local support simulation</strong>
            <small>No external messages are sent.</small>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <span className="breadcrumb">
            Support operations <ChevronRight size={14} aria-hidden />{" "}
            <strong>{page}</strong>
          </span>
          <div className="top-actions">
            <Badge tone={stale ? "amber" : "green"}>
              {stale ? "Connection stale" : "Connected locally"}
            </Badge>
            <button
              className="button quiet token-button"
              onClick={() => setShowToken(true)}
            >
              <KeyRound size={16} aria-hidden />
              {token ? "Operator connected" : "Connect operator"}
            </button>
          </div>
        </header>
        <div className={`control-strip ${control?.enabled ? "enabled" : ""}`}>
          <div>
            <ShieldCheck size={19} aria-hidden />
            <span>
              <strong>
                Automatic answers {control?.enabled ? "enabled" : "stopped"}
              </strong>
              <small>
                {control?.enabled
                  ? "Every answer still requires all checks and an atomic release."
                  : "Tickets still process and receive safe handoffs."}
              </small>
            </span>
          </div>
          <button
            className={`button ${control?.enabled ? "danger" : "secondary"}`}
            onClick={() => {
              setReason("");
              setControlOpen(true);
            }}
          >
            {control?.enabled ? (
              <Square size={15} aria-hidden />
            ) : (
              <ShieldCheck size={16} aria-hidden />
            )}
            {control?.enabled
              ? "Stop automatic answers"
              : "Re-enable automation"}
          </button>
        </div>
        <main id="main" tabIndex={-1}>
          {status?.generation_mode === "paid_test" && (
            <div className="alert warning">
              <strong>Paid development test mode</strong> · {status?.provider?.configured_model}
              <p>Shared budget: USD {status?.paid_budget?.limit_usd ?? "unavailable"}; remaining USD {status?.paid_budget?.remaining_usd ?? "unavailable"}. No paid fallback or budget reset.
                {status?.paid_budget?.blocked ? " Paid requests are blocked; inspect Settings." : " Every answer still requires all checks."}</p>
            </div>
          )}
          <div className="page-heading">
            <div>
              <div className="eyebrow">CLOUDSERVE / OPERATIONS</div>
              <h1 ref={heading} tabIndex={-1}>
                {selected ? "Ticket detail" : page}
              </h1>
              <p>
                {page === "Overview"
                  ? "A clear view of every request, decision, and handoff."
                  : page === "New ticket"
                    ? "Submit a support request through the real four-channel workflow."
                    : page === "Runs & evaluation"
                      ? "Process complete files and inspect durable, reconciled results."
                      : "Inspect the evidence. Keep the next step clear."}
              </p>
            </div>
            <div className="heading-actions">
              <button
                className="button quiet"
                aria-label="Refresh backend state"
                onClick={() => void refresh()}
              >
                <RefreshCw size={17} aria-hidden />
                Refresh
              </button>
              {page !== "New ticket" && (
                <button
                  className="button primary"
                  onClick={() => navigate("New ticket")}
                >
                  <Plus size={18} aria-hidden />
                  New ticket
                </button>
              )}
            </div>
          </div>
          <div aria-live="polite" className="announcements">
            {error && (
              <div role="alert" className="alert error">
                <span>{error}</span>
                <button aria-label="Dismiss error" onClick={() => setError("")}>
                  <X size={18} />
                </button>
              </div>
            )}
            {notice && (
              <div className="alert success">
                <span>{notice}</span>
                <button
                  aria-label="Dismiss notification"
                  onClick={() => setNotice("")}
                >
                  <X size={18} />
                </button>
              </div>
            )}
          </div>
          {stale && (
            <div className="alert warning">
              Backend state could not be refreshed. Values may be stale; no
              success is assumed.
            </div>
          )}
          {page === "Overview" && (
            <>
              <div className="metric-grid">
                {[
                  [
                    "Loaded tickets",
                    String(ticketTotal),
                    "Persisted queue records",
                  ],
                  [
                    "Answered",
                    String(answered),
                    "Committed simulated releases",
                  ],
                  ["Escalated", String(escalated), "Human handling required"],
                  [
                    "Blocked drafts",
                    String(blocked),
                    "Included within escalations",
                  ],
                ].map(([label, value, caption]) => (
                  <section className="metric-card" key={label}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <small>{caption}</small>
                  </section>
                ))}
              </div>
              <p className="small muted">
                Outcome counts show the current queue page ({tickets.length}{" "}
                records); total ticket count covers the full queue.
              </p>
              <div className="overview-grid">
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Recent requests</h2>
                      <p>Actual backend state, refreshed every 1.8 seconds</p>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => navigate("Ticket queue")}
                    >
                      View queue <ArrowUpRight size={15} aria-hidden />
                    </button>
                  </div>
                  {tickets.length ? (
                    ticketTable(tickets.slice(0, 5))
                  ) : (
                    <Empty title="Your queue is clear">
                      Submit a ticket or import a development file to start
                      processing.
                    </Empty>
                  )}
                </section>
                <section className="panel system-panel">
                  <div className="panel-heading">
                    <h2>System readiness</h2>
                    <Activity size={19} aria-hidden />
                  </div>
                  <div className="readiness-row">
                    <span>API process</span>
                    <Badge tone={status ? "green" : "amber"}>
                      {status ? "Online" : "Connecting"}
                    </Badge>
                  </div>
                  <div className="readiness-row">
                    <span>Local assets / worker</span>
                    <Badge tone={health?.ready ? "green" : "amber"}>
                      {health?.ready
                        ? "Ready"
                        : health
                          ? "Preparing / unavailable"
                          : "Unknown"}
                    </Badge>
                  </div>
                  <div className="readiness-row">
                    <span>Runtime mode</span>
                    <Badge>{status?.mode ?? "Unavailable"}</Badge>
                  </div>
                  <div className="readiness-row">
                    <span>Generation provider</span>
                    <Badge
                      tone={status?.status === "configured" ? "green" : "amber"}
                    >
                      {status?.status === "configured"
                        ? "Configured"
                        : "Degraded"}
                    </Badge>
                  </div>
                  <div className="readiness-row">
                    <span>Decider safety gate</span>
                    <Badge
                      tone={
                        status?.decider?.mode === "enforce" &&
                        status?.decider?.policy_ready
                          ? "green"
                          : "amber"
                      }
                    >
                      {human(status?.decider?.mode)}
                    </Badge>
                  </div>
                  <div className="readiness-row">
                    <span>Decider policy</span>
                    <Badge
                      tone={status?.decider?.policy_ready ? "green" : "amber"}
                    >
                      {status?.decider?.policy_ready
                        ? "Artifact ready"
                        : "Not ready"}
                    </Badge>
                  </div>
                  <p className="small muted">
                    {status?.decider?.mode === "shadow"
                      ? "Shadow evaluation — cannot authorize release"
                      : status?.decider?.mode === "disabled"
                        ? "Decider disabled — cannot authorize release"
                        : status?.decider?.mode === "enforce" &&
                            status?.decider?.policy_ready
                          ? "Enforced safety gate — all release checks still apply"
                          : "Decider policy unavailable — cannot authorize release"}
                  </p>
                  <p className="small wrap">
                    {status?.provider?.configured_model ?? status?.provider?.model ?? "Model not configured"}
                  </p>
                  {(status?.issues ?? []).map((issue: string) => (
                    <div className="setup-issue" key={issue}>
                      {issue}
                    </div>
                  ))}
                  <details>
                    <summary>Artifact and provider details</summary>
                    <pre>
                      {JSON.stringify(
                        {
                          classifier: status?.classifier,
                          retrieval: status?.retrieval,
                          provider: status?.provider,
                          decider: status?.decider,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                  <a
                    className="external-link"
                    href="http://localhost:3000"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open Grafana <ArrowUpRight size={15} aria-hidden />
                  </a>
                </section>
              </div>
              <section className="workflow-note">
                <ShieldCheck size={24} aria-hidden />
                <div>
                  <h3>Answers earn their release.</h3>
                  <p>
                    Normalize → classify → retrieve → route → generate → check →
                    commit. An unavailable check or unsafe draft produces a
                    contextual handoff.
                  </p>
                </div>
              </section>
            </>
          )}
          {page === "New ticket" && (
            <div className="new-grid">
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>Request intake</h2>
                    <p>
                      Simulated channels · no inbox connection or outbound
                      delivery
                    </p>
                  </div>
                  <Badge>Schema v1</Badge>
                </div>
                <div
                  className="channel-tabs"
                  role="group"
                  aria-label="Ticket channel"
                >
                  {channels.map((c) => (
                    <button
                      key={c.id}
                      aria-pressed={channel === c.id}
                      className={channel === c.id ? "selected" : ""}
                      onClick={() => setChannel(c.id)}
                    >
                      <c.icon size={18} aria-hidden />
                      {c.name}
                    </button>
                  ))}
                </div>
                <form onSubmit={submit} className="ticket-form">
                  <div className="form-top">
                    <span className="small muted">
                      {channels.find((c) => c.id === channel)?.name} request
                    </span>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        setSubject(examples[channel].subject);
                        setBody(examples[channel].body);
                      }}
                    >
                      Use synthetic example
                    </button>
                  </div>
                  <label>
                    Subject <span className="optional">optional</span>
                    <input
                      value={subject}
                      maxLength={1000}
                      onChange={(e) => setSubject(e.target.value)}
                      placeholder={
                        channel === "forum"
                          ? "Community topic"
                          : "Short description of the issue"
                      }
                    />
                  </label>
                  <label>
                    {channel === "docs_comment"
                      ? "Documentation comment"
                      : channel === "chat"
                        ? "Customer message"
                        : "Request body"}
                    <textarea
                      value={body}
                      maxLength={20000}
                      rows={8}
                      onChange={(e) => setBody(e.target.value)}
                      aria-invalid={!!formError}
                      aria-describedby="body-help body-error"
                      placeholder="Describe the issue and what has already been tried."
                    />
                  </label>
                  <div id="body-help" className="field-help">
                    Up to 20,000 characters. Do not include passwords or API
                    keys.
                  </div>
                  {formError && (
                    <p id="body-error" className="field-error">
                      {formError}
                    </p>
                  )}
                  <details className="identity-fields">
                    <summary>Optional customer identifiers</summary>
                    <p className="small muted">
                      Kept in protected local input and sanitized before
                      provider calls.
                    </p>
                    <div className="two-fields">
                      <label>
                        Customer name
                        <input
                          value={customerName}
                          onChange={(e) => setCustomerName(e.target.value)}
                          autoComplete="off"
                        />
                      </label>
                      <label>
                        Customer ID
                        <input
                          value={customerId}
                          onChange={(e) => setCustomerId(e.target.value)}
                          autoComplete="off"
                        />
                      </label>
                    </div>
                  </details>
                  <div className="form-footer">
                    <span>
                      <ShieldCheck size={16} aria-hidden /> Every decision is
                      persisted
                    </span>
                    <button
                      className="button primary"
                      disabled={busy}
                      type="submit"
                    >
                      {busy ? "Submitting…" : "Submit ticket"}
                      <ChevronRight size={17} aria-hidden />
                    </button>
                  </div>
                </form>
              </section>
              <aside className="panel explanatory">
                <span className="section-icon">
                  <ShieldCheck size={25} aria-hidden />
                </span>
                <h2>
                  Support workflow,
                  <br />
                  with a safety boundary.
                </h2>
                <p>
                  Local classifiers identify intent and urgency. Retrieved
                  documentation provides the evidence.
                </p>
                <ol>
                  <li>
                    <strong>Information requests</strong>
                    <span>
                      Eligible requests may receive a checked, cited answer.
                    </span>
                  </li>
                  <li>
                    <strong>Sensitive or uncertain requests</strong>
                    <span>
                      Become contextual handoffs with an explicit reason.
                    </span>
                  </li>
                  <li>
                    <strong>No silent closure</strong>
                    <span>
                      A simulated answer is not evidence that an issue was
                      resolved.
                    </span>
                  </li>
                </ol>
                <div className="mini-notice">
                  {status?.provider?.status === "missing_key"
                    ? "OpenRouter key missing. Local classification, retrieval and handoffs still run; generation is unavailable."
                    : "Generation uses the selected model. Provider failures, low confidence or insufficient sources produce an explicit handoff."}
                </div>
              </aside>
            </div>
          )}
          {(page === "Ticket queue" || page === "Handoffs") && !selected && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>
                    {page === "Handoffs"
                      ? "Needs human attention"
                      : "All requests"}
                  </h2>
                  <p>
                    {ticketTotal} persisted records matching the current filters
                  </p>
                </div>
                <Badge>
                  {page === "Handoffs"
                    ? "Internal review workflow"
                    : "Four-channel intake"}
                </Badge>
              </div>
              <div className="filters">
                <label className="search-field">
                  <span className="sr-only">Search tickets</span>
                  <Search size={17} aria-hidden />
                  <input
                    placeholder="Search safe request summaries"
                    value={search}
                    onChange={(e) => {
                      setSearch(e.target.value);
                      setOffset(0);
                    }}
                  />
                </label>
                <label>
                  <span className="sr-only">Channel filter</span>
                  <select
                    value={channelFilter}
                    onChange={(e) => {
                      setChannelFilter(e.target.value);
                      setOffset(0);
                    }}
                  >
                    <option value="">All channels</option>
                    {channels.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </label>
                {page !== "Handoffs" && (
                  <label>
                    <span className="sr-only">Outcome filter</span>
                    <select
                      value={actionFilter}
                      onChange={(e) => {
                        setActionFilter(e.target.value);
                        setOffset(0);
                      }}
                    >
                      <option value="">All outcomes</option>
                      <option value="auto_respond">Answered</option>
                      <option value="escalate">Escalated</option>
                    </select>
                  </label>
                )}
                <label>
                  <span className="sr-only">Urgency filter</span>
                  <select
                    value={urgencyFilter}
                    onChange={(e) => {
                      setUrgencyFilter(e.target.value);
                      setOffset(0);
                    }}
                  >
                    <option value="">All urgency</option>
                    {["high", "medium", "low"].map((u) => (
                      <option key={u}>{u}</option>
                    ))}
                  </select>
                </label>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={blockedFilter}
                    onChange={(e) => {
                      setBlockedFilter(e.target.checked);
                      setOffset(0);
                    }}
                  />
                  Blocked only
                </label>
              </div>
              {tickets.length ? (
                ticketTable(tickets)
              ) : (
                <Empty
                  title={
                    page === "Handoffs"
                      ? "No handoffs match"
                      : "No requests match"
                  }
                >
                  Change your filters or submit a new ticket.
                </Empty>
              )}
              <div className="pagination">
                <span>
                  {ticketTotal ? offset + 1 : 0}–
                  {Math.min(offset + tickets.length, ticketTotal)} of{" "}
                  {ticketTotal}
                </span>
                <div>
                  <button
                    className="button quiet"
                    disabled={!offset}
                    onClick={() => setOffset(Math.max(0, offset - 25))}
                  >
                    Previous
                  </button>
                  <button
                    className="button quiet"
                    disabled={offset + 25 >= ticketTotal}
                    onClick={() => setOffset(offset + 25)}
                  >
                    Next
                  </button>
                </div>
              </div>
            </section>
          )}
          {selected && (page === "Ticket queue" || page === "Handoffs") && (
            <>
              <button
                className="text-button back"
                onClick={() => {
                  setSelected(null);
                  setDetail(null);
                }}
              >
                <ArrowLeft size={16} aria-hidden />
                Back to queue
              </button>
              {!detail ? (
                <section className="panel loading">
                  Loading persisted ticket…
                </section>
              ) : (
                <>
                  <div className="detail-meta">
                    <code>{selected}</code>
                    <Badge>{human(detail.ticket?.channel)}</Badge>
                    <Badge
                      tone={
                        outcome?.blocked
                          ? "red"
                          : outcome?.action === "auto_respond"
                            ? "green"
                            : "amber"
                      }
                    >
                      {outcome?.blocked
                        ? "Blocked · escalated"
                        : outcome
                          ? human(outcome.action)
                          : (detail.status ?? "Processing")}
                    </Badge>
                    <Badge>
                      {outcome?.provenance === "Live" &&
                      status?.status === "degraded"
                        ? "Live local workflow · provider degraded"
                        : (outcome?.provenance ?? "Awaiting outcome")}
                    </Badge>
                  </div>
                  <div className="detail-grid">
                    <div className="detail-main">
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Safe request</h2>
                          <button
                            className="text-button"
                            onClick={() => void showOriginal()}
                          >
                            <KeyRound size={14} aria-hidden />
                            View protected original
                          </button>
                          <Badge>Operator evidence</Badge>
                        </div>
                        <div className="panel-body">
                          <h3>
                            {detail.ticket?.subject || "No subject supplied"}
                          </h3>
                          <p className="preserve">
                            {detail.ticket?.body ||
                              "Safe request summary unavailable."}
                          </p>
                        </div>
                      </section>
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Customer preview</h2>
                          {outcome?.answer && (
                            <button
                              className="text-button"
                              onClick={() => void copy(outcome.answer!)}
                            >
                              <Copy size={15} aria-hidden />
                              Copy answer
                            </button>
                          )}
                        </div>
                        <div className="panel-body">
                          {outcome?.action === "auto_respond" &&
                          outcome.answer ? (
                            <>
                              <Badge tone="green">
                                Committed simulated release
                              </Badge>
                              <p className="preserve answer">
                                {outcome.answer}
                              </p>
                            </>
                          ) : outcome ? (
                            <>
                              <Badge tone="amber">Human help required</Badge>
                              <p>
                                This request needs a support operator’s review.
                                No answer has been automatically released.
                              </p>
                              <p className="small muted">
                                This is a local acknowledgment preview. No
                                message was sent.
                              </p>
                            </>
                          ) : (
                            <>
                              <Badge>Processing</Badge>
                              <p>
                                A customer answer appears here only after every
                                check passes and release is durably committed.
                              </p>
                            </>
                          )}
                        </div>
                      </section>
                      {outcome?.handoff && (
                        <section className="panel">
                          <div className="panel-heading">
                            <h2>Contextual handoff</h2>
                            <button
                              className="text-button"
                              onClick={() =>
                                void copy(
                                  JSON.stringify(outcome.handoff, null, 2),
                                )
                              }
                            >
                              <Copy size={15} aria-hidden />
                              Copy handoff
                            </button>
                          </div>
                          <div className="panel-body">
                            <h3>{outcome.handoff.summary}</h3>
                            <p>{outcome.handoff.reason}</p>
                            <div className="label-value">
                              <span>Troubleshooting supplied</span>
                              <p>{outcome.handoff.troubleshooting}</p>
                            </div>
                            <div className="label-value">
                              <span>Missing context</span>
                              <ul>
                                {(outcome.handoff.missing_context ?? []).map(
                                  (q, i) => (
                                    <li key={i}>{q}</li>
                                  ),
                                )}
                              </ul>
                            </div>
                            <div className="label-value">
                              <span>Unresolved questions</span>
                              <ul>
                                {(
                                  outcome.handoff.unresolved_questions ?? []
                                ).map((q, i) => (
                                  <li key={i}>{q}</li>
                                ))}
                              </ul>
                            </div>
                            <p className="small muted">
                              Summary provenance:{" "}
                              {human(outcome.handoff.provenance)}
                            </p>
                            <p className="small muted">
                              Internal review status:{" "}
                              {human(detail.review?.status ?? "open")}
                            </p>
                            <form
                              onSubmit={(e) => {
                                e.preventDefault();
                                void mutate(
                                  () =>
                                    api(`/tickets/${selected}/review`, {
                                      method: "POST",
                                      body: JSON.stringify({
                                        status: reviewStatus,
                                        notes: reviewNotes,
                                      }),
                                    }),
                                  "Internal review recorded. This does not confirm customer resolution.",
                                );
                              }}
                              className="review-form"
                            >
                              <label>
                                Review status
                                <select
                                  value={reviewStatus}
                                  onChange={(e) =>
                                    setReviewStatus(e.target.value)
                                  }
                                >
                                  <option value="open">Open</option>
                                  <option value="in_review">In review</option>
                                  <option value="reviewed">Reviewed</option>
                                </select>
                              </label>
                              <label>
                                Internal review notes
                                <textarea
                                  rows={2}
                                  value={reviewNotes}
                                  onChange={(e) =>
                                    setReviewNotes(e.target.value)
                                  }
                                  maxLength={2000}
                                />
                              </label>
                              <button
                                className="button secondary"
                                disabled={busy || !token}
                                type="submit"
                              >
                                Save internal review
                              </button>
                              <small>
                                Review status is internal, not a resolution
                                metric.
                              </small>
                            </form>
                          </div>
                        </section>
                      )}
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Decision timeline</h2>
                          <Badge>Durable audit</Badge>
                        </div>
                        <ol className="timeline">
                          {(detail.events ?? []).map((ev, i) => (
                            <li key={ev.decision_id ?? i}>
                              <span className="timeline-dot" />
                              <div>
                                <strong>{human(ev.stage)}</strong>
                                <small>
                                  {ev.timestamp ??
                                    ev.created_at ??
                                    "Timestamp unavailable"}
                                </small>
                                <details>
                                  <summary>Decision details</summary>
                                  <pre>
                                    {JSON.stringify(ev.data ?? ev, null, 2)}
                                  </pre>
                                </details>
                              </div>
                            </li>
                          ))}
                          {!detail.events?.length && (
                            <li>Waiting for persisted stage events.</li>
                          )}
                        </ol>
                      </section>
                    </div>
                    <aside className="detail-side">
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Classification</h2>
                        </div>
                        <div className="panel-body">
                          <div className="label-value">
                            <span>Intent</span>
                            {dimension(outcome?.classification.intent)}
                          </div>
                          <div className="label-value">
                            <span>Urgency · advisory only</span>
                            {dimension(outcome?.classification.urgency)}
                            <p className="small muted">
                              An operator must confirm priority. Development
                              urgency quality did not support automatic SLA
                              decisions.
                            </p>
                          </div>
                          <p className="small muted">
                            {outcome?.classification.calibration_status ??
                              "Local classifier result pending"}
                          </p>
                        </div>
                      </section>
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Routing decision</h2>
                        </div>
                        <div className="panel-body">
                          <p>
                            {outcome?.reason ??
                              "Processing has not reached a final outcome."}
                          </p>
                          <p className="small muted">
                            {outcome?.route.reason_codes.join(" · ")}
                          </p>
                        </div>
                      </section>
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Retrieved evidence</h2>
                          <Badge>{outcome?.sources?.length ?? 0}</Badge>
                        </div>
                        {outcome?.sources?.length ? (
                          sources(outcome.sources ?? [])
                        ) : (
                          <p className="panel-body muted">
                            No supporting passages available.
                          </p>
                        )}
                      </section>
                      <section className="panel">
                        <div className="panel-heading">
                          <h2>Blocking checks</h2>
                        </div>
                        <div className="checks">
                          {outcome?.checks?.map((c, i) => (
                            <div key={i}>
                              <span>{human(c.name)}</span>
                              <Badge
                                tone={
                                  c.status === "pass"
                                    ? "green"
                                    : c.status === "fail"
                                      ? "red"
                                      : "amber"
                                }
                              >
                                {human(c.status)}
                              </Badge>
                              {(c.reasons?.length ?? 0) > 0 && (
                                <small>{c.reasons?.join(" ")}</small>
                              )}
                            </div>
                          ))}
                          {!outcome?.checks?.length && (
                            <p className="muted">
                              No attempted-answer checks recorded.
                            </p>
                          )}
                        </div>
                      </section>
                    </aside>
                  </div>
                </>
              )}
            </>
          )}
          {page === "Settings" && (
            <OperatorSettingsPanel
              api={api}
              token={token}
              automationEnabled={!!control?.enabled}
              onConnect={() => setShowToken(true)}
              onSaved={() => void refresh()}
            />
          )}
          {page === "Runs & evaluation" && (
            <>
              <BundledDatasetPanel
                api={api}
                token={token}
                automationEnabled={!!control?.enabled}
                onConnect={() => setShowToken(true)}
                onLaunch={async (result) => {
                  setRunId(result.run_id);
                  setNotice(
                    `Bundled development run queued with ${result.expected_count} input positions.`,
                  );
                  await refresh();
                }}
              />
              <div className="new-grid">
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Import a ticket file</h2>
                      <p>Unattended execution through the shared pipeline</p>
                    </div>
                    <FileJson size={21} aria-hidden />
                  </div>
                  <form className="panel-body upload-form" onSubmit={upload}>
                    <label className="upload-area">
                      <Upload size={28} aria-hidden />
                      <strong>Choose a JSON file</strong>
                      <span>Array · up to 10 MiB · 10,000 records</span>
                      <input
                        type="file"
                        accept=".json,application/json"
                        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                      />
                    </label>
                    {file && (
                      <p className="small">
                        Selected: {file.name} ({(file.size / 1024).toFixed(1)}{" "}
                        KiB)
                      </p>
                    )}
                    <details>
                      <summary>Exact accepted JSON format</summary>
                      <p className="small muted">
                        Upload a UTF-8 JSON array of ticket objects. Each ticket
                        requires channel (email, chat, docs_comment, or forum)
                        and a nonempty body up to 20,000 characters. Subject is
                        optional, up to 1,000 characters. Optional ticket_id and
                        ISO received_at are preserved. Maximum file size is 10
                        MiB and maximum array length is 10,000. Duplicate ticket
                        IDs remain separate input positions. Malformed
                        individual records produce explicit input-error
                        handoffs; an invalid top-level file is rejected.
                        Evaluation labels and history never become prediction
                        input.
                      </p>
                      <pre>
                        {JSON.stringify(
                          [
                            {
                              channel: "email",
                              subject: "API retry guidance",
                              body: "How should I retry an HTTP 429 response?",
                            },
                          ],
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                    <p className="small muted">
                      Malformed records receive explicit escalations. Duplicate
                      external IDs remain separate input positions. Closing the
                      browser does not cancel a run.
                    </p>
                    <button
                      className="button primary"
                      type="submit"
                      disabled={busy || !file}
                    >
                      {busy ? "Queuing…" : "Launch development run"}
                      <ChevronRight size={17} aria-hidden />
                    </button>
                  </form>
                </section>
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Before supplied validation</h2>
                    <ShieldCheck size={20} aria-hidden />
                  </div>
                  <div className="panel-body">
                    <p>
                      Final validation is a separate, frozen CLI run. This
                      upload launches development processing.
                    </p>
                    <ul className="checklist">
                      <li>Freeze code, locks, prompts, models, and policy</li>
                      <li>Verify free model access and adequate quota</li>
                      <li>
                        Keep supplied validation unopened during development
                      </li>
                      <li>
                        Use a new empty evaluation cache and output directory
                      </li>
                      <li>Retain the first run and report any interruption</li>
                    </ul>
                    <p className="small muted">
                      Human review, confirmed closure, and fairness remain
                      unavailable until independent evidence exists.
                    </p>
                  </div>
                </section>
              </div>
              <section className="panel runs-panel">
                <div className="panel-heading">
                  <h2>Persisted runs</h2>
                  <Badge>{runs.length} loaded</Badge>
                </div>
                {runs.length ? (
                  <div
                    className="table-scroll"
                    role="region"
                    aria-label="Run history"
                    tabIndex={0}
                  >
                    <table>
                      <thead>
                        <tr>
                          <th>Run</th>
                          <th>State</th>
                          <th>Progress</th>
                          <th>Type</th>
                          <th>Inspect</th>
                        </tr>
                      </thead>
                      <tbody>
                        {runs.map((r) => (
                          <tr key={r.run_id}>
                            <td>
                              <code>{r.run_id}</code>
                              <small>{r.created_at ?? r.started_at}</small>
                            </td>
                            <td>
                              <Badge
                                tone={
                                  r.state === "completed"
                                    ? "green"
                                    : [
                                          "failed",
                                          "incomplete",
                                          "interrupted",
                                        ].includes(r.state)
                                      ? "red"
                                      : "amber"
                                }
                              >
                                {human(r.state)}
                              </Badge>
                            </td>
                            <td>
                              {r.processed_count ?? 0} / {r.expected_count ?? 0}
                              <progress
                                aria-label="Run progress"
                                value={r.processed_count ?? 0}
                                max={Math.max(1, r.expected_count ?? 0)}
                              />
                            </td>
                            <td>{human(r.kind ?? r.type)}</td>
                            <td>
                              <button
                                className="text-button"
                                onClick={() => setRunId(r.run_id)}
                              >
                                Inspect <ChevronRight size={15} aria-hidden />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <Empty title="No run yet">
                    Import a development file to produce real progress and
                    outcomes.
                  </Empty>
                )}
              </section>
              {runId && (
                <section className="panel runs-panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Run evidence</h2>
                      <code>{runId}</code>
                    </div>
                    <Badge>{human(runDetail?.state)}</Badge>
                  </div>
                  <div className="panel-body">
                    <p>
                      Reconciliation:{" "}
                      <strong>
                        {runMetrics?.governance?.reconciled
                          ? "Complete"
                          : runMetrics
                            ? "Incomplete"
                            : "Loading"}
                      </strong>{" "}
                      · {runDetail?.processed_count ?? 0} /{" "}
                      {runDetail?.expected_count ?? 0} outcomes
                    </p>
                    <p className="small muted">
                      Heartbeat:{" "}
                      {runDetail?.heartbeat ??
                        runDetail?.heartbeat_at ??
                        "Unavailable"}{" "}
                      · Queue/quota wait:{" "}
                      {runDetail?.next_eligible_at ??
                        "No scheduled wait reported"}
                    </p>
                    {runDetail?.error && (
                      <div className="alert error">
                        {String(runDetail.error)}
                      </div>
                    )}
                    <div className="three-metrics">
                      <div>
                        <h3>Business</h3>
                        <p>
                          Auto-answer rate{" "}
                          <strong>
                            {pct(runMetrics?.business?.auto_answer_rate)}
                          </strong>
                        </p>
                        <p>
                          Confirmed FCR{" "}
                          <strong>
                            {pct(runMetrics?.business?.confirmed_fcr)}
                          </strong>
                        </p>
                      </div>
                      <div>
                        <h3>Technical</h3>
                        <p>
                          Processing median{" "}
                          <strong>
                            {seconds(
                              runMetrics?.technical?.processing_median_seconds,
                            )}
                          </strong>
                        </p>
                        <p>
                          Processing p95{" "}
                          <strong>
                            {seconds(
                              runMetrics?.technical?.processing_p95_seconds,
                            )}
                          </strong>
                        </p>
                      </div>
                      <div>
                        <h3>Governance</h3>
                        <p>
                          Outcome coverage{" "}
                          <strong>
                            {pct(runMetrics?.governance?.outcome_coverage)}
                          </strong>
                        </p>
                        <p>
                          Fairness quality{" "}
                          <strong>Independent review required</strong>
                        </p>
                      </div>
                    </div>
                    <details>
                      <summary>Full three-tier metrics</summary>
                      <pre>{JSON.stringify(runMetrics, null, 2)}</pre>
                    </details>
                    <div className="artifact-buttons">
                      {[
                        "manifest.json",
                        "outcomes.jsonl",
                        "decisions.jsonl",
                        "metrics.json",
                        "human_review_sample.csv",
                      ].map((n) => (
                        <button
                          className="button secondary"
                          key={n}
                          onClick={() => void exportArtifact(n)}
                        >
                          {n}
                          <ArrowUpRight size={14} aria-hidden />
                        </button>
                      ))}
                    </div>
                    <p className="small muted">
                      Artifacts may require the documented harness export. An
                      unavailable download reports an error.
                    </p>
                  </div>
                </section>
              )}
            </>
          )}
          <footer className="footer">
            <span>CloudServe Support · Local capstone application</span>
            <span>
              {lastSync
                ? `Last sync ${lastSync.toLocaleTimeString()}`
                : "Waiting for backend"}
            </span>
          </footer>
        </main>
      </div>
      <dialog
        ref={dialog}
        onCancel={() => {
          setControlOpen(false);
          setShowToken(false);
        }}
        onClose={() => {
          setControlOpen(false);
          setShowToken(false);
        }}
      >
        <div className="dialog-heading">
          <h2>
            {showToken
              ? "Connect local operator"
              : control?.enabled
                ? "Stop automatic answers"
                : "Re-enable automatic answers"}
          </h2>
          <button
            className="icon-button"
            aria-label="Close dialog"
            onClick={() => {
              setControlOpen(false);
              setShowToken(false);
            }}
          >
            <X size={20} />
          </button>
        </div>
        {showToken ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setShowToken(false);
              setNotice("Operator token is held only in this page’s memory.");
            }}
          >
            <p>
              Use the operator token configured in the local server environment.
              It is never stored in browser storage.
            </p>
            <label>
              Operator token
              <input
                autoFocus
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
            <div className="dialog-actions">
              <button
                type="button"
                className="button quiet"
                onClick={() => {
                  setToken("");
                  setShowToken(false);
                }}
              >
                Disconnect
              </button>
              <button type="submit" className="button primary">
                Use token
              </button>
            </div>
          </form>
        ) : (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (!token) {
                setShowToken(true);
                return;
              }
              const done = await mutate(
                () =>
                  api("/controls/automation", {
                    method: "PUT",
                    body: JSON.stringify({
                      enabled: !control?.enabled,
                      reason,
                    }),
                  }),
                control?.enabled
                  ? "Automatic answers stopped. In-flight releases are subject to the new epoch."
                  : "Automation explicitly enabled.",
              );
              if (done) setControlOpen(false);
            }}
          >
            <p>
              {control?.enabled
                ? "Stopping persists a new control epoch. In-flight tickets cannot release after a stop/re-enable cycle."
                : "Re-enabling requires frozen assets, verified model access and an available budget in paid-test mode. It does not revive drafts affected by a previous stop."}
            </p>
            <label>
              Operator reason
              <textarea
                autoFocus
                required
                minLength={3}
                maxLength={500}
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <div className="dialog-actions">
              <button
                type="button"
                className="button quiet"
                onClick={() => setControlOpen(false)}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busy || reason.trim().length < 3}
                className={`button ${control?.enabled ? "danger" : "primary"}`}
              >
                {busy
                  ? "Saving…"
                  : control?.enabled
                    ? "Stop automatic answers"
                    : "Verify and re-enable"}
              </button>
            </div>
          </form>
        )}
      </dialog>
      <dialog
        ref={originalDialog}
        className="source-dialog"
        aria-labelledby="original-title"
        onCancel={clearOriginal}
        onClose={clearOriginal}
      >
        <div className="dialog-heading">
          <h2 id="original-title">Protected original request</h2>
          <button
            className="icon-button"
            aria-label="Close original request"
            onClick={clearOriginal}
          >
            <X size={20} />
          </button>
        </div>
        <p>
          Local operator inspection only. This may contain customer identifiers.
          It is excluded from the customer preview. Closing clears this view
          from component state.
        </p>
        {original && (
          <pre className="source-text">{JSON.stringify(original, null, 2)}</pre>
        )}
      </dialog>
      <dialog
        ref={sourceDialog}
        className="source-dialog"
        onCancel={() => setSource(null)}
        onClose={() => setSource(null)}
      >
        <div className="dialog-heading">
          <h2>Source passage</h2>
          <button
            className="icon-button"
            aria-label="Close source"
            onClick={() => setSource(null)}
          >
            <X size={20} />
          </button>
        </div>
        {source && (
          <>
            <h3>{source.title}</h3>
            <code>{source.chunk_id}</code>
            <p className="small muted">
              {source.doc_id} · offsets {source.start}–{source.end} · corpus{" "}
              {source.corpus_hash}
            </p>
            <pre className="source-text">{source.content}</pre>
            <p className="small muted">
              Exact frozen source text. Markdown is displayed as text; raw HTML
              is never executed.
            </p>
          </>
        )}
      </dialog>
    </div>
  );
  function ticketTable(items: Ticket[]) {
    return (
      <div
        className="table-scroll"
        role="region"
        aria-label="Support ticket queue"
        tabIndex={0}
      >
        <table>
          <thead>
            <tr>
              <th>Request</th>
              <th>Channel</th>
              <th>Urgency</th>
              <th>Outcome</th>
              <th>
                <span className="sr-only">Open ticket</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.record_id}>
                <td>
                  <button
                    className="request-link"
                    onClick={() => {
                      setPage("Ticket queue");
                      setSelected(t.record_id);
                      setDetail(null);
                    }}
                  >
                    {t.ticket?.subject ||
                      t.ticket?.body?.slice(0, 90) ||
                      "Request awaiting normalization"}
                  </button>
                  <small>
                    {t.record_id.slice(0, 8)} ·{" "}
                    {human(t.outcome?.classification.intent?.label)}
                  </small>
                </td>
                <td>{human(t.ticket?.channel)}</td>
                <td>
                  <Badge
                    tone={
                      t.outcome?.classification.urgency?.label === "high"
                        ? "red"
                        : "neutral"
                    }
                  >
                    {human(t.outcome?.classification.urgency?.label)}
                  </Badge>
                </td>
                <td>
                  <Badge
                    tone={
                      t.outcome?.blocked
                        ? "red"
                        : t.outcome?.action === "auto_respond"
                          ? "green"
                          : t.outcome
                            ? "amber"
                            : "neutral"
                    }
                  >
                    {t.outcome?.blocked
                      ? "Blocked · escalated"
                      : t.outcome?.action === "auto_respond"
                        ? "Answered"
                        : t.outcome
                          ? "Escalated"
                          : human(t.status) || "Queued"}
                  </Badge>
                </td>
                <td>
                  <button
                    className="icon-button"
                    aria-label={`Open ticket ${t.record_id}`}
                    onClick={() => {
                      setPage("Ticket queue");
                      setSelected(t.record_id);
                      setDetail(null);
                    }}
                  >
                    <ChevronRight size={18} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
}

type ApiRequest = (path: string, options?: RequestInit) => Promise<any>;
type OperatorValues = {
  generation_mode: "free_only" | "paid_test";
  generation_model: string;
  decider_model: string;
  decider_mode: "disabled" | "shadow" | "enforce";
  timeout_seconds: string;
  max_retries: string;
};
function OperatorSettingsPanel({
  api,
  token,
  automationEnabled,
  onConnect,
  onSaved,
}: {
  api: ApiRequest;
  token: string;
  automationEnabled: boolean;
  onConnect: () => void;
  onSaved: () => void;
}) {
  const [saved, setSaved] = useState<Obj | null>(null),
    [values, setValues] = useState<OperatorValues | null>(null),
    [catalog, setCatalog] = useState<Obj | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [loading, setLoading] = useState(false),
    [saving, setSaving] = useState(false),
    [discovering, setDiscovering] = useState(false),
    [paidAcknowledged, setPaidAcknowledged] = useState(false);
  const errorRef = useRef<HTMLDivElement>(null);
  function accept(data: Obj) {
    setSaved(data);
    setPaidAcknowledged(false);
    setValues({
      generation_mode: data.generation_mode ?? "free_only",
      generation_model: data.generation_model,
      decider_model: data.decider_model,
      decider_mode: data.decider_mode,
      timeout_seconds: String(data.timeout_seconds),
      max_retries: String(data.max_retries),
    });
  }
  useEffect(() => {
    let active = true;
    setSaved(null);
    setValues(null);
    setCatalog(null);
    setError("");
    setNotice("");
    if (token) {
      setLoading(true);
      api("/settings")
        .then((data) => {
          if (active) accept(data);
        })
        .catch((e) => {
          if (active) setError((e as Error).message);
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    } else setLoading(false);
    return () => {
      active = false;
    };
  }, [api, token]);
  async function discover() {
    setDiscovering(true);
    setError("");
    try {
      const data = await api("/models");
      setCatalog(data);
      setNotice(
        `Free catalog discovery returned ${data.generation?.length ?? 0} generation and ${data.decider?.length ?? 0} Decider choices. Saving still re-verifies the selection.`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDiscovering(false);
    }
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    setNotice("");
    setError("");
    if (!values) return;
    if (values.generation_mode === "paid_test" && !paidAcknowledged) {
      setError("Acknowledge paid development testing before saving.");
      errorRef.current?.focus();
      return;
    }
    if (automationEnabled) {
      setError("Stop automatic answers before saving settings.");
      return;
    }
    if (values.decider_mode === "enforce" && !saved?.policy_ready) {
      setError(
        "Enforce mode requires a calibrated, frozen Decider policy. Use shadow evaluation until that policy is ready.",
      );
      errorRef.current?.focus();
      return;
    }
    setSaving(true);
    try {
      const data = await api("/settings", {
        method: "PUT",
        body: JSON.stringify({
          ...values,
          timeout_seconds: Number(values.timeout_seconds),
          max_retries: Number(values.max_retries),
        }),
      });
      accept(data);
      setNotice(
        `Settings saved durably at revision ${data.revision}. Automatic answers remain stopped.`,
      );
      onSaved();
    } catch (e) {
      setError((e as Error).message);
      setTimeout(() => errorRef.current?.focus(), 0);
    } finally {
      setSaving(false);
    }
  }
  function change(field: keyof OperatorValues, value: string) {
    if (values) setValues({ ...values, [field]: value });
    setError("");
    setNotice("");
  }
  function changeGenerationMode(mode: "free_only" | "paid_test") {
    if (!values) return;
    setValues({ ...values, generation_mode: mode,
      generation_model: mode === "paid_test" ? "openai/gpt-4.1-mini" :
        (saved?.generation_model?.endsWith(":free") ? saved.generation_model : "google/gemma-4-26b-a4b-it:free"),
      max_retries: mode === "paid_test" ? "0" : "2" });
    setPaidAcknowledged(false);setError("");setNotice("");
  }
  return (
    <div className="new-grid">
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2 id="operator-settings-heading">Runtime settings</h2>
            <p>Explicit model policy · no automatic paid fallback</p>
          </div>
          <Settings2 size={20} aria-hidden />
        </div>
        <div className="panel-body">
          <p className="small muted">
            Stop automatic answers and wait for every queued or running batch to
            finish before saving. A change invalidates the final evaluation
            freeze. Price/capability and budget verification is required before
            settings are accepted.
          </p>
          {!token ? (
            <>
              <p>Connect the local operator to load and edit settings.</p>
              <button className="button secondary" onClick={onConnect}>
                Connect operator for settings
              </button>
            </>
          ) : loading ? (
            <p role="status">Loading persisted settings…</p>
          ) : null}
          <div ref={errorRef} tabIndex={-1}>
            {error && (
              <div className="alert error" role="alert">
                {error}
              </div>
            )}
          </div>
          {notice && (
            <div className="alert success" role="status">
              {notice}
            </div>
          )}
          {values && (
            <form
              className="ticket-form settings-form"
              aria-labelledby="operator-settings-heading"
              method="post"
              action="/api/v1/settings"
              onSubmit={save}
            >
              <label htmlFor="generation-mode">Generation mode
                <select id="generation-mode" name="generation_mode" value={values.generation_mode}
                  onChange={(e) => changeGenerationMode(e.target.value as "free_only" | "paid_test")}>
                  <option value="free_only">Free-only</option>
                  <option value="paid_test">Paid development test</option>
                </select>
              </label>
              {values.generation_mode === "paid_test" && (
                <div className="alert warning">
                  <p>Paid requests use the shared USD {saved?.paid_budget?.limit_usd ?? "0.25"} development allowance, including prior use. There is no automatic top-up or reset. Maximum retries are zero.</p>
                  <label className="paid-consent"><input type="checkbox" name="paid_acknowledged" required checked={paidAcknowledged}
                    onChange={(e) => setPaidAcknowledged(e.target.checked)} />I acknowledge paid development testing within the shared USD0.25 cap.</label>
                </div>
              )}
              <label htmlFor="generation-model">
                {values.generation_mode === "paid_test" ? "Paid generation model" : "Generation model — free ID required"}
                {values.generation_mode === "paid_test" ? (
                  <select id="generation-model" name="generation_model" value={values.generation_model}
                    onChange={(e) => change("generation_model",e.target.value)}>
                    <option value="openai/gpt-4.1-mini">GPT-4.1 Mini</option>
                    <option value="openai/gpt-4o-mini">GPT-4o Mini</option>
                  </select>
                ) : <input
                  id="generation-model"
                  name="generation_model"
                  value={values.generation_model}
                  onChange={(e) => change("generation_model", e.target.value)}
                  list="generation-model-choices"
                  required
                  pattern=".+:free"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  aria-describedby="model-help"
                />}
              </label>
              <datalist id="generation-model-choices">
                {catalog?.generation?.map((m: Obj) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </datalist>
              <label htmlFor="decider-model">
                Decider model — free ID required
                <input
                  id="decider-model"
                  name="decider_model"
                  value={values.decider_model}
                  onChange={(e) => change("decider_model", e.target.value)}
                  list="decider-model-choices"
                  required
                  pattern=".+:free"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  aria-describedby="model-help"
                />
              </label>
              <datalist id="decider-model-choices">
                {catalog?.decider?.map((m: Obj) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </datalist>
              <p className="field-help" id="model-help">
                Choose a discovered model or enter a :free ID manually. Unknown
                pricing prevents saving. Free-only mode rejects paid routes; paid testing permits only the two bounded choices. Discovery and verification inspect metadata; they do not prove inference quality.
              </p>
              <button
                className="button secondary"
                type="button"
                onClick={() => void discover()}
                disabled={discovering || saving}
              >
                {discovering
                  ? "Checking free catalog…"
                  : "Discover free models"}
              </button>
              {catalog && (
                <p className="small muted">
                  Catalog source: {human(catalog.source)}. {catalog.note}
                </p>
              )}
              <label htmlFor="decider-mode">
                Decider mode
                <select
                  id="decider-mode"
                  name="decider_mode"
                  value={values.decider_mode}
                  onChange={(e) => change("decider_mode", e.target.value)}
                  aria-describedby="decider-mode-help"
                >
                  <option value="disabled">Disabled</option>
                  <option value="shadow">Shadow evaluation</option>
                  <option value="enforce" disabled={!saved?.policy_ready}>
                    Enforce{" "}
                    {saved?.policy_ready ? "" : "— calibrated policy required"}
                  </option>
                </select>
              </label>
              <p className="field-help" id="decider-mode-help">
                {saved?.policy_ready
                  ? "A ready policy does not bypass any other release check."
                  : "Enforce unavailable: no calibrated, frozen Decider policy is ready. Shadow evaluation cannot authorize release. Model probability is not calibrated correctness."}
              </p>
              <div className="two-fields">
                <label htmlFor="model-timeout">
                  Model timeout (seconds)
                  <input
                    type="number"
                    id="model-timeout"
                    name="timeout_seconds"
                    value={values.timeout_seconds}
                    onChange={(e) => change("timeout_seconds", e.target.value)}
                    required
                    min={5}
                    max={120}
                    step={1}
                    inputMode="numeric"
                  />
                </label>
                <label htmlFor="model-retries">
                  Maximum retries
                  <input
                    type="number"
                    id="model-retries"
                    name="max_retries"
                    value={values.max_retries}
                    onChange={(e) => change("max_retries", e.target.value)}
                    required
                    min={0}
                    max={2}
                    disabled={values.generation_mode === "paid_test"}
                    step={1}
                    inputMode="numeric"
                  />
                </label>
              </div>
              {automationEnabled && (
                <div className="alert warning">
                  Automatic answers are enabled. Stop them using the global
                  control before saving settings.
                </div>
              )}
              <div className="form-footer">
                <span>
                  Saving requires no active batch and the selected model policy preflight.
                </span>
                <button
                  className="button primary"
                  type="submit"
                  disabled={saving || discovering || automationEnabled}
                >
                  {saving ? "Verifying and saving…" : "Save settings"}
                </button>
              </div>
            </form>
          )}
        </div>
      </section>
      <aside className="panel">
        <div className="panel-heading">
          <h2>Persisted configuration</h2>
          <ShieldCheck size={20} aria-hidden />
        </div>
        <div className="readiness-row">
          <span>Generation mode</span><Badge>{human(saved?.generation_mode ?? "free_only")}</Badge>
        </div>
        {saved?.paid_budget && (
          <div className="panel-body">
            <h3>Shared paid-test budget</h3>
            <p>Limit: USD {saved.paid_budget.limit_usd}</p>
            <p>Spent: USD {saved.paid_budget.spent_usd ?? "unavailable"}</p>
            <p>Reserved: USD {saved.paid_budget.reserved_usd ?? "unavailable"}</p>
            <p>Remaining: USD {saved.paid_budget.remaining_usd ?? "unavailable"}</p>
            <p>{saved.paid_budget.blocked ? `Blocked: ${human(saved.paid_budget.reason)}` : "Persists across tickets and restarts. No reset or top-up."}</p>
          </div>
        )}
        <div className="readiness-row">
          <span>Settings revision</span>
          <Badge>{saved?.revision ?? "Unavailable"}</Badge>
        </div>
        <div className="readiness-row">
          <span>Source</span>
          <Badge>{human(saved?.source)}</Badge>
        </div>
        <div className="readiness-row">
          <span>Provider key configured</span>
          <Badge tone={saved?.credential_configured ? "green" : "amber"}>
            {saved
              ? saved.credential_configured
                ? "Configured"
                : "Not configured"
              : "Unavailable"}
          </Badge>
        </div>
        <div className="readiness-row">
          <span>Decider policy ready</span>
          <Badge>
            {saved ? (saved.policy_ready ? "Yes" : "No") : "Unavailable"}
          </Badge>
        </div>
        <div className="panel-body">
          <p className="small muted">
            Updated: {saved?.updated_at ?? "No operator update recorded"}
          </p>
          <p className="small muted">
            API keys remain in the local server environment. Keys cannot be
            viewed or edited here. Restarting the application reads the
            persisted configuration; settings changes remain auditable.
          </p>
        </div>
      </aside>
    </div>
  );
}
function BundledDatasetPanel({
  api,
  token,
  automationEnabled,
  onConnect,
  onLaunch,
}: {
  api: ApiRequest;
  token: string;
  automationEnabled: boolean;
  onConnect: () => void;
  onLaunch: (result: Obj) => Promise<void>;
}) {
  const [datasets, setDatasets] = useState<Obj[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [selection, setSelection] = useState("all"),
    [limit, setLimit] = useState("10"),
    [busy, setBusy] = useState(false),
    [downloading, setDownloading] = useState(false);
  useEffect(() => {
    let active = true;
    setDatasets([]);
    setError("");
    if (token) {
      setLoading(true);
      api("/datasets")
        .then((data) => {
          if (active) setDatasets(data.items ?? []);
        })
        .catch((e) => {
          if (active) setError((e as Error).message);
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    } else setLoading(false);
    return () => {
      active = false;
    };
  }, [api, token]);
  const development = datasets.find((d) => d.id === "development");
  async function launch(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (!token) {
      onConnect();
      return;
    }
    if (!automationEnabled) {
      setError(
        "Enable automatic answers explicitly before launching a bundled development run. Provider and budget preflight plus all release checks still apply.",
      );
      return;
    }
    setBusy(true);
    try {
      const result = await api("/datasets/launch", {
        method: "POST",
        body: JSON.stringify({
          dataset_id: "development",
          limit: selection === "all" ? null : Number(limit),
        }),
      });
      await onLaunch(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function template() {
    if (!token) {
      onConnect();
      return;
    }
    setDownloading(true);
    setError("");
    try {
      const response = await fetch("/api/v1/datasets/template", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok)
        throw Error(
          "Template download unavailable. Check the operator connection and try again.",
        );
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "cloudserve-ticket-template.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  }
  return (
    <section className="panel runs-panel bundled-dataset-panel">
      <div className="panel-heading">
        <div>
          <h2 id="bundled-dataset-heading">Bundled development data</h2>
          <p>
            Supplied development tickets only · final validation is never
            selectable here
          </p>
        </div>
        <FileJson size={20} aria-hidden />
      </div>
      <div className="panel-body">
        {!token ? (
          <>
            <p>
              Connect the local operator to discover bundled development data or
              download the sample template.
            </p>
            <button className="button secondary" onClick={onConnect}>
              Connect operator for datasets
            </button>
          </>
        ) : loading ? (
          <p role="status">Checking bundled dataset availability…</p>
        ) : development ? (
          <p className="small">
            <strong>{development.name}</strong> · {development.record_count}{" "}
            records · {development.available ? "Available" : "Unavailable"}.{" "}
            {development.description}
          </p>
        ) : (
          <p className="small muted">
            No approved bundled development dataset is available.
          </p>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
        <form
          className="ticket-form bundled-dataset-form"
          aria-labelledby="bundled-dataset-heading"
          method="post"
          action="/api/v1/datasets/launch"
          onSubmit={launch}
        >
          <label htmlFor="development-record-selection">
            Development record selection
            <select
              id="development-record-selection"
              name="selection"
              value={selection}
              onChange={(e) => setSelection(e.target.value)}
            >
              <option value="all">
                All {development?.record_count ?? 500} development tickets
              </option>
              <option value="first">First N development tickets</option>
            </select>
          </label>
          {selection === "first" && (
            <label htmlFor="development-record-limit">
              Number of first records
              <input
                id="development-record-limit"
                name="limit"
                type="number"
                required
                min={1}
                max={development?.record_count ?? 500}
                step={1}
                inputMode="numeric"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </label>
          )}
          <p className="small muted">
            The operator chooses the count before launch. The server verifies
            the frozen development-data fingerprint, creates a durable run, and
            processes it after browser closure. This is a development run; it
            does not satisfy final supplied-validation evidence.
          </p>
          {!automationEnabled && (
            <p className="small muted">
              Automatic answers are stopped. Explicit enable and successful
              free-provider preflight are required to launch bundled data.
            </p>
          )}
          <div className="artifact-buttons">
            <button
              className="button primary"
              type="submit"
              disabled={busy || loading || !token || !development?.available}
            >
              {busy
                ? "Queuing development data…"
                : "Launch bundled development run"}
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={downloading || !token}
              onClick={() => void template()}
            >
              {downloading ? "Downloading…" : "Download sample JSON template"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}
