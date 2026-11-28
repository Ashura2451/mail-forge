import { useEffect, useState } from "react";
import {
  api,
  bootstrapSession,
  exportComparison,
  exportSortSelection,
  upload,
  uploadAsset,
  uploadComparison,
  type ApiRecord,
} from "./api/client";
import "./App.css";

type Navigate = (target: string) => void;
type Action = (work: () => Promise<unknown>, message: string) => void;
type SortMode = "all" | "selected" | "none" | "records";
type SortRow = {
  record_key: string;
  email: string;
  original_input: string;
  source_file: string;
  source_record_position: number;
  associated_data: Record<string, unknown>;
  provider: string;
  domain: string;
  registered_domain: string;
  mail_host_state: string;
  disposition: string;
  status: string;
  valid: boolean;
  reason_codes: string[];
};

const primaryViews = [
  ["sort", "Addresses"],
  ["compare", "Compare"],
  ["send", "Compose"],
];
const utilityViews = [
  ["history", "Activity"],
  ["accounts", "Accounts"],
  ["saved", "Saved lists"],
  ["settings", "Settings"],
];
const titles: Record<string, { label: string; description: string }> = {
  dashboard: {
    label: "Files & lists",
    description: "Open a file or choose a direct operation.",
  },
  sort: {
    label: "Addresses",
    description: "Inspect, group and select the records in a file.",
  },
  compare: {
    label: "Compare",
    description: "Put lists side by side and keep the result explicit.",
  },
  send: {
    label: "Compose",
    description:
      "Build a message, inspect its audience, then review the operation.",
  },
  history: {
    label: "Activity",
    description: "Automatic records of preparation, progress and recovery.",
  },
  accounts: {
    label: "Accounts",
    description: "Configure a sender only when you need one.",
  },
  saved: { label: "Saved lists", description: "Reusable local address lists." },
  settings: {
    label: "Settings",
    description: "Local storage and compatibility details.",
  },
};

function viewFromPath(pathname: string) {
  const segment = pathname.replace(/^\/+/, "").split("/")[0];
  return titles[segment] ? segment : "dashboard";
}

function apiPath(view: string) {
  return (
    (
      {
        dashboard: "/dashboard",
        sort: "/list-tools/jobs",
        compare: "/list-comparison/history",
        send: "/campaigns",
        history: "/campaigns",
        accounts: "/providers",
        saved: "/lists",
        settings: "/runtime",
      } as Record<string, string>
    )[view] || "/dashboard"
  );
}

function App() {
  const [view, setView] = useState(() =>
    viewFromPath(window.location.pathname),
  );
  const [data, setData] = useState<ApiRecord>({});
  const [setup, setSetup] = useState<ApiRecord>({});
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(() =>
    utilityViews.some(([id]) => id === view),
  );

  const load = async (target = view) => {
    setError("");
    try {
      setData(await api<ApiRecord>(apiPath(target)));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Unable to load this view",
      );
    }
  };
  const navigate: Navigate = (target) => {
    const [nextView, query = ""] = target.split("?");
    const url =
      nextView === "dashboard"
        ? "/"
        : `/${nextView}${query ? `?${query}` : ""}`;
    window.history.pushState({}, "", url);
    setView(nextView);
    setMobileNav(false);
    if (utilityViews.some(([id]) => id === nextView)) setToolsOpen(true);
    void load(nextView);
  };
  const run: Action = (work, message) => {
    setBusy(true);
    setError("");
    void work()
      .then(() => {
        setNotice(message);
        return load(viewFromPath(window.location.pathname));
      })
      .catch((caught) =>
        setError(
          caught instanceof Error
            ? caught.message
            : "The local operation failed",
        ),
      )
      .finally(() => setBusy(false));
  };

  useEffect(() => {
    const onPop = () => {
      const next = viewFromPath(window.location.pathname);
      setView(next);
      void load(next);
    };
    window.addEventListener("popstate", onPop);
    bootstrapSession()
      .then(async () => {
        setSetup(await api("/setup"));
        await load(viewFromPath(window.location.pathname));
      })
      .catch((caught) =>
        setError(
          caught instanceof Error
            ? caught.message
            : "The local service is unavailable",
        ),
      )
      .finally(() => setLoading(false));
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  useEffect(() => {
    if (!mobileNav) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNav(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileNav]);

  const meta = titles[view] || titles.dashboard;
  const content =
    view === "dashboard" ? (
      <Home
        data={data}
        setup={setup}
        onSetup={() =>
          run(async () => {
            const result = await api("/setup/complete", {
              method: "POST",
              body: JSON.stringify({
                timezone: "UTC",
                simulation_mode: true,
                sample_data: false,
              }),
            });
            setSetup(result);
            return result;
          }, "Local workspace is ready")
        }
        onNavigate={navigate}
      />
    ) : view === "sort" ? (
      <Sorter data={data} onRun={run} onNavigate={navigate} />
    ) : view === "compare" ? (
      <CompareLists data={data} onRun={run} onNavigate={navigate} />
    ) : view === "send" ? (
      <Sender onRun={run} onNavigate={navigate} />
    ) : view === "history" ? (
      <Activity data={data} onNavigate={navigate} />
    ) : view === "accounts" ? (
      <Accounts data={data} onRun={run} />
    ) : view === "saved" ? (
      <SavedLists data={data} onNavigate={navigate} />
    ) : (
      <Settings data={data} onRun={run} />
    );

  return (
    <div className="app-shell">
      <aside
        className={`desk-nav ${mobileNav ? "is-open" : ""}`}
        aria-label="Mailforge navigation"
      >
        <div className="wordmark">
          <button
            className="wordmark-button"
            onClick={() => navigate("dashboard")}
          >
            <span className="wordmark-mark">M</span>
            <span>Mailforge</span>
          </button>
          <small>local email utility</small>
        </div>
        <nav aria-label="Main tools" className="primary-nav">
          <span className="nav-caption">Work with</span>
          {primaryViews.map(([id, label]) => (
            <button
              key={id}
              className={view === id ? "nav-link active" : "nav-link"}
              aria-current={view === id ? "page" : undefined}
              onClick={() => navigate(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="nav-tools">
          <button
            className="tools-toggle"
            aria-expanded={toolsOpen}
            onClick={() => setToolsOpen((open) => !open)}
          >
            <span>More tools</span>
            <span aria-hidden="true">{toolsOpen ? "−" : "+"}</span>
          </button>
          {toolsOpen && (
            <nav aria-label="Supporting tools">
              {utilityViews.map(([id, label]) => (
                <button
                  key={id}
                  className={view === id ? "nav-link active" : "nav-link"}
                  aria-current={view === id ? "page" : undefined}
                  onClick={() => navigate(id)}
                >
                  {label}
                </button>
              ))}
            </nav>
          )}
        </div>
        <div className="nav-foot">
          <span className="local-dot" /> Local only
          <small>Files stay in this workspace</small>
        </div>
      </aside>
      {mobileNav && (
        <button
          className="scrim"
          aria-label="Close navigation"
          onClick={() => setMobileNav(false)}
        />
      )}
      <main className="main-area" aria-busy={loading || busy}>
        <header className="appbar">
          <div className="appbar-title">
            <button
              className="menu-button"
              aria-label={mobileNav ? "Close navigation" : "Open navigation"}
              aria-expanded={mobileNav}
              onClick={() => setMobileNav((open) => !open)}
            >
              Menu
            </button>
            <div>
              <p className="eyebrow">MAILFORGE / LOCAL</p>
              <h1>{meta.label}</h1>
              <p className="appbar-description">{meta.description}</p>
            </div>
          </div>
          <div className="appbar-actions">
            <span className="local-badge">
              <span className="local-dot" /> Local
            </span>
            <button
              className="quiet-button"
              onClick={() => void load()}
              disabled={busy}
            >
              Refresh
            </button>
          </div>
        </header>
        {notice && (
          <div className="notice" role="status">
            <span>{notice}</span>
            <button
              aria-label="Dismiss notification"
              onClick={() => setNotice("")}
            >
              Dismiss
            </button>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            <span>{error}</span>
            <button onClick={() => void load()}>Retry</button>
            <button aria-label="Dismiss error" onClick={() => setError("")}>
              Dismiss
            </button>
          </div>
        )}
        {loading ? (
          <div className="state-panel" role="status">
            <strong>Opening {meta.label.toLowerCase()}…</strong>
            <span>Reading the local workspace.</span>
          </div>
        ) : (
          content
        )}
      </main>
    </div>
  );
}

function Home({
  data,
  setup,
  onSetup,
  onNavigate,
}: {
  data: ApiRecord;
  setup: ApiRecord;
  onSetup: () => void;
  onNavigate: Navigate;
}) {
  const counts = data.counts || {};
  return (
    <div className="workspace home-workspace">
      <section className="welcome-strip">
        <div>
          <p className="eyebrow teal">FILES & LISTS</p>
          <h2>Start with the object you have.</h2>
          <p>
            Open an address file, compare lists, or compose directly. The
            supporting records are kept automatically.
          </p>
        </div>
        <div className="workspace-note">
          <strong>Private by default</strong>
          <span>Local files and durable records stay here.</span>
        </div>
      </section>
      <section className="operation-grid" aria-label="Independent operations">
        <OperationCard
          label="Addresses"
          title="Sort and group a file"
          text="See provider distribution, inspect every row, and export or transfer an exact selection."
          action="Open Addresses"
          onClick={() => onNavigate("sort")}
          accent
        />
        <OperationCard
          label="Compare"
          title="Put lists side by side"
          text="Subtract, find overlap, merge unique addresses, or check successful previous sends."
          action="Open Compare"
          onClick={() => onNavigate("compare")}
        />
        <OperationCard
          label="Compose"
          title="Prepare a message"
          text="Paste recipients or import a file, bring HTML, add files, preview, and review the scope."
          action="Open Compose"
          onClick={() => onNavigate("send")}
        />
      </section>
      <section className="home-lower">
        <div className="panel compact-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">WORKSPACE</p>
              <h3>Useful context</h3>
            </div>
            <button className="text-link" onClick={() => onNavigate("history")}>
              Open Activity
            </button>
          </div>
          <div className="metric-row">
            <Metric label="Saved addresses" value={counts.contacts ?? 0} />
            <Metric label="Saved lists" value={counts.lists ?? 0} />
            <Metric
              label="Recent operations"
              value={data.campaigns?.length ?? 0}
            />
          </div>
        </div>
        {!setup.complete && (
          <div className="callout">
            <div>
              <strong>Optional local setup</strong>
              <p>
                Sorting and comparison work now. Save workspace preferences only
                when you need them.
              </p>
            </div>
            <button className="primary-button" onClick={onSetup}>
              Finish setup
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

function OperationCard({
  label,
  title,
  text,
  action,
  onClick,
  accent = false,
}: {
  label: string;
  title: string;
  text: string;
  action: string;
  onClick: () => void;
  accent?: boolean;
}) {
  return (
    <article className={`operation-card ${accent ? "accent" : ""}`}>
      <span className="card-label">{label}</span>
      <h3>{title}</h3>
      <p>{text}</p>
      <button className="text-link" onClick={onClick}>
        {action} <span aria-hidden="true">→</span>
      </button>
    </article>
  );
}
function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
function Empty({
  title,
  text,
  action,
  onAction,
}: {
  title: string;
  text: string;
  action?: string;
  onAction?: () => void;
}) {
  return (
    <div className="empty-state">
      <span className="empty-mark" aria-hidden="true">
        ○
      </span>
      <div>
        <strong>{title}</strong>
        <p>{text}</p>
        {action && onAction && (
          <button className="text-link" onClick={onAction}>
            {action} →
          </button>
        )}
      </div>
    </div>
  );
}
function StatusTag({ value }: { value: string }) {
  return (
    <span className={`status-tag ${value.toLowerCase().replaceAll(" ", "-")}`}>
      {value}
    </span>
  );
}
function Panel({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <section className={`panel ${className}`}>{children}</section>;
}
function PanelHeading({
  eyebrow,
  title,
  detail,
  action,
}: {
  eyebrow?: string;
  title: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="panel-heading">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h3>{title}</h3>
        {detail && <p className="panel-detail">{detail}</p>}
      </div>
      {action}
    </div>
  );
}
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function Sorter({
  data,
  onRun,
  onNavigate,
}: {
  data: ApiRecord;
  onRun: Action;
  onNavigate: Navigate;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [profile, setProfile] = useState("full");
  const [dnsMode, setDnsMode] = useState("offline");
  const [preview, setPreview] = useState<ApiRecord | null>(null);
  const [job, setJob] = useState<ApiRecord | null>(null);
  const [rows, setRows] = useState<SortRow[]>([]);
  const [query, setQuery] = useState("");
  const [disposition, setDisposition] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [sortMode, setSortMode] = useState<SortMode>("all");
  const [selectedProviders, setSelectedProviders] = useState<string[]>([]);
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set());
  const [individualMode, setIndividualMode] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const jobs: ApiRecord[] = data.items || [];
  const providerCounts: Record<string, number> = job?.summary?.providers || {};
  const providers = Object.keys(providerCounts).sort((a, b) =>
    a.localeCompare(b),
  );
  const selectedCount =
    sortMode === "all"
      ? (job?.accepted_count ?? 0)
      : sortMode === "none"
        ? 0
        : sortMode === "records"
          ? selectedRows.size
          : selectedProviders.reduce(
              (sum, provider) => sum + Number(providerCounts[provider] || 0),
              0,
            );
  const totalRows = job?.summary?.counts?.total ?? 0;
  const loadJob = async (id: string) => {
    const next = await api<ApiRecord>(`/list-tools/jobs/${id}`);
    setJob(next);
    return next;
  };
  const loadRows = async (id: string, nextOffset = 0) => {
    const params = new URLSearchParams({
      limit: "100",
      offset: String(nextOffset),
    });
    if (query) params.set("q", query);
    if (providerFilter) params.set("provider", providerFilter);
    if (disposition) params.set("disposition", disposition);
    const result = await api<ApiRecord>(
      `/list-tools/jobs/${id}/rows?${params}`,
    );
    setRows(result.items || []);
    setOffset(nextOffset);
    return result;
  };
  useEffect(() => {
    if (!job || !["running", "pending", "queued"].includes(job.status)) return;
    const timer = window.setInterval(() => {
      void loadJob(job.id);
    }, 750);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (job?.status === "completed") void loadRows(job.id, 0);
  }, [job?.id, job?.status, query, providerFilter, disposition]);
  const previewFile = () =>
    file &&
    onRun(async () => {
      const result = await upload("/list-tools/preview", file);
      setPreview(result);
      return result;
    }, "Preview ready; the original file was not changed");
  const start = () =>
    file &&
    onRun(async () => {
      const created = await upload(
        "/list-tools/jobs",
        file,
        `?profile=${encodeURIComponent(profile)}&dns_mode=${encodeURIComponent(dnsMode)}`,
      );
      await api(`/list-tools/jobs/${created.id}/start`, { method: "POST" });
      await loadJob(created.id);
      setSortMode("all");
      setSelectedProviders([]);
      setSelectedRows(new Set());
      setIndividualMode(false);
      return created;
    }, "Sorting started");
  const selectProvider = (provider: string) => {
    setIndividualMode(false);
    setSelectedRows(new Set());
    if (sortMode === "all") {
      setSelectedProviders(providers.filter((item) => item !== provider));
      setSortMode("selected");
      return;
    }
    const next = selectedProviders.includes(provider)
      ? selectedProviders.filter((item) => item !== provider)
      : [...selectedProviders, provider];
    setSelectedProviders(next);
    setSortMode(
      next.length
        ? next.length === providers.length
          ? "all"
          : "selected"
        : "none",
    );
  };
  const selectAllProviders = () => {
    setIndividualMode(false);
    setSelectedRows(new Set());
    setSelectedProviders(providers);
    setSortMode("all");
  };
  const clearProviders = () => {
    setIndividualMode(false);
    setSelectedRows(new Set());
    setSelectedProviders([]);
    setSortMode("none");
  };
  const startIndividual = () => {
    setIndividualMode(true);
    setSelectedRows(new Set());
    setSortMode("records");
    setSelectedProviders([]);
  };
  const toggleRow = (row: SortRow) => {
    setIndividualMode(true);
    setSortMode("records");
    setSelectedProviders([]);
    setSelectedRows((previous) => {
      const next = new Set(previous);
      if (next.has(row.record_key)) next.delete(row.record_key);
      else next.add(row.record_key);
      return next;
    });
  };
  const selectionPayload = () => ({
    provider_keys: sortMode === "selected" ? selectedProviders : [],
    record_keys: sortMode === "records" ? [...selectedRows] : [],
    selection_mode: sortMode,
  });
  const handoff = (destination: string) =>
    job &&
    onRun(
      async () => {
        if (
          sortMode === "none" ||
          (sortMode === "records" && selectedRows.size === 0)
        )
          throw new Error("Choose at least one address before transferring it");
        const result = await api<ApiRecord>(
          `/list-tools/jobs/${job.id}/handoff`,
          {
            method: "POST",
            body: JSON.stringify({
              ...selectionPayload(),
              name: saveName || undefined,
            }),
          },
        );
        if (destination !== "sort")
          onNavigate(
            `${destination}?list_id=${encodeURIComponent(result.list?.id || "")}`,
          );
        return result;
      },
      destination === "sort"
        ? "Exact selection saved"
        : `Exact selection ready in ${destination === "compare" ? "Compare" : "Compose"}`,
    );
  const exportRows = () =>
    job &&
    onRun(async () => {
      if (
        sortMode === "none" ||
        (sortMode === "records" && selectedRows.size === 0)
      )
        throw new Error("Choose at least one address before exporting");
      const blob = await exportSortSelection(
        job.id,
        sortMode === "selected" ? selectedProviders : [],
        sortMode,
        sortMode === "records" ? [...selectedRows] : [],
      );
      downloadBlob(blob, "mailforge-addresses.csv");
    }, "Selection exported with source data");
  const saveSelection = () => {
    setSaveOpen(false);
    if (job) handoff("sort");
  };
  const processAction = (action: string, message: string) =>
    job &&
    onRun(async () => {
      const result = await api<ApiRecord>(
        `/list-tools/jobs/${job.id}/${action}`,
        { method: "POST" },
      );
      setJob(result);
      return result;
    }, message);
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">ADDRESS FILE</p>
          <h2>Inspect the records, then choose a scope.</h2>
          <p className="lede">
            Provider groups are the fast path. The table is the detailed path.
            Filtering never changes selection.
          </p>
        </div>
        <span className="context-badge">No account needed</span>
      </div>
      <Panel className="import-panel">
        <PanelHeading
          eyebrow="1 / OPEN"
          title="Bring in a file"
          detail="TXT, CSV, JSON and JSONL are supported. The source is retained for provenance."
        />
        <div className="import-row">
          <label className="file-picker">
            <span>{file ? file.name : "Choose an address file"}</span>
            <input
              aria-label="Address file"
              type="file"
              accept=".txt,.csv,.json,.jsonl"
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
          </label>
          <Field label="Profile">
            <select
              value={profile}
              onChange={(event) => setProfile(event.target.value)}
            >
              <option value="full">Classify and validate</option>
              <option value="validation">Validate only</option>
              <option value="dedupe">Duplicates and validity</option>
              <option value="classify">Provider groups</option>
            </select>
          </Field>
          <Field
            label="Mail-host detail"
            hint={
              dnsMode === "offline" ? "Visible domain only" : "May take longer"
            }
          >
            <select
              value={dnsMode}
              onChange={(event) => setDnsMode(event.target.value)}
            >
              <option value="offline">Do not resolve hosts</option>
              <option value="sync">Resolve mail hosts</option>
            </select>
          </Field>
          <div className="button-group">
            <button onClick={previewFile} disabled={!file}>
              Preview
            </button>
            <button className="primary-button" onClick={start} disabled={!file}>
              Sort file
            </button>
          </div>
        </div>
        {preview && (
          <div className="preview-summary">
            <Metric label="Accepted" value={preview.accepted ?? 0} />
            <Metric label="Invalid" value={preview.invalid ?? 0} />
            <Metric label="Duplicates" value={preview.duplicates ?? 0} />
            <span>Preview only · no saved list created</span>
          </div>
        )}
      </Panel>
      {!job && jobs.length > 0 && (
        <Panel className="recent-panel">
          <PanelHeading
            title="Recent address files"
            detail="Reopen a completed local result."
          />
          <div className="recent-list">
            {jobs.slice(0, 5).map((item) => (
              <button
                key={item.id}
                className="recent-item"
                onClick={() => void loadJob(item.id)}
              >
                <span>
                  <strong>{item.source_filename}</strong>
                  <small>
                    {item.status} · {item.accepted_count ?? 0} accepted
                  </small>
                </span>
                <span aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </Panel>
      )}
      {job && (
        <>
          <Panel className="result-panel">
            <div className="result-toolbar">
              <div>
                <p className="eyebrow teal">2 / WORKSPACE</p>
                <h3>{job.source_filename}</h3>
                <p className="panel-detail">
                  {job.status} · {job.processed_count ?? 0} examined ·{" "}
                  {job.accepted_count ?? 0} usable unique addresses
                </p>
              </div>
              <div className="toolbar-actions">
                <StatusTag
                  value={
                    job.status === "completed"
                      ? "Completed"
                      : job.status === "running"
                        ? "Running"
                        : job.status === "paused"
                          ? "Paused"
                          : job.status
                  }
                />
                {job.status === "running" && (
                  <button
                    onClick={() => processAction("pause", "Processing paused")}
                  >
                    Pause
                  </button>
                )}
                {job.status === "paused" && (
                  <button
                    onClick={() =>
                      processAction("resume", "Processing resumed")
                    }
                  >
                    Resume
                  </button>
                )}
                {["running", "paused"].includes(job.status) && (
                  <button
                    className="danger-button"
                    onClick={() =>
                      processAction("cancel", "Processing canceled")
                    }
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
            {job.status !== "completed" ? (
              <div className="progress-state">
                <progress
                  max={totalRows || undefined}
                  value={
                    totalRows
                      ? Math.min(job.processed_count || 0, totalRows)
                      : undefined
                  }
                />
                <strong>
                  {totalRows ? (
                    <>
                      {Math.min(job.processed_count || 0, totalRows)} of{" "}
                      {totalRows} records examined
                    </>
                  ) : (
                    <>
                      {job.processed_count || 0} records examined; total is
                      still being calculated
                    </>
                  )}
                </strong>
                <span>
                  Mailforge keeps the operation resumable; this table appears
                  when the result is complete.
                </span>
              </div>
            ) : (
              <>
                <div className="scope-strip">
                  <div>
                    <span className="eyebrow">CURRENT SCOPE</span>
                    <strong>
                      {selectedCount.toLocaleString()}{" "}
                      {selectedCount === 1 ? "address" : "addresses"}
                    </strong>
                    <small>
                      {sortMode === "all"
                        ? "All usable unique records"
                        : sortMode === "selected"
                          ? "Selected provider groups"
                          : sortMode === "records"
                            ? "Individually selected records"
                            : "Nothing selected"}
                    </small>
                  </div>
                  <div className="button-group">
                    <button
                      onClick={() => setSaveOpen(true)}
                      disabled={
                        sortMode === "none" ||
                        (sortMode === "records" && selectedRows.size === 0)
                      }
                    >
                      Save exact selection
                    </button>
                    <button
                      onClick={exportRows}
                      disabled={
                        sortMode === "none" ||
                        (sortMode === "records" && selectedRows.size === 0)
                      }
                    >
                      Export selection
                    </button>
                    <button
                      onClick={() => handoff("compare")}
                      disabled={
                        sortMode === "none" ||
                        (sortMode === "records" && selectedRows.size === 0)
                      }
                    >
                      Compare
                    </button>
                    <button
                      className="primary-button"
                      onClick={() => handoff("send")}
                      disabled={
                        sortMode === "none" ||
                        (sortMode === "records" && selectedRows.size === 0)
                      }
                    >
                      Compose
                    </button>
                  </div>
                </div>
                <div className="sorter-layout">
                  <aside className="provider-panel">
                    <PanelHeading
                      eyebrow="GROUPS"
                      title="Mail-host classification"
                      detail="Groups use the classifier, not a string match on the visible domain."
                    />
                    <div className="provider-actions">
                      <button
                        onClick={selectAllProviders}
                        className={sortMode === "all" ? "selected" : ""}
                      >
                        All
                      </button>
                      <button
                        onClick={clearProviders}
                        className={sortMode === "none" ? "selected" : ""}
                      >
                        None
                      </button>
                      <button
                        onClick={startIndividual}
                        className={sortMode === "records" ? "selected" : ""}
                      >
                        Choose rows
                      </button>
                    </div>
                    <div className="provider-list">
                      {providers.map((provider) => (
                        <label className="provider-row" key={provider}>
                          <input
                            type="checkbox"
                            checked={
                              sortMode === "all" ||
                              (sortMode === "selected" &&
                                selectedProviders.includes(provider))
                            }
                            onChange={() => selectProvider(provider)}
                            disabled={sortMode === "records"}
                          />
                          <span>
                            <strong>{provider}</strong>
                            <small>
                              {providerCounts[provider]} usable unique
                            </small>
                          </span>
                        </label>
                      ))}
                    </div>
                    <div className="classification-note">
                      <strong>Unknown host is not invalid</strong>
                      <p>
                        Malformed addresses are excluded. A valid address whose
                        host was not resolved remains available for review or
                        export.
                      </p>
                    </div>
                  </aside>
                  <div className="address-panel">
                    <div className="table-tools">
                      <Field label="Find">
                        <input
                          value={query}
                          onChange={(event) => setQuery(event.target.value)}
                          placeholder="Email, domain or source"
                        />
                      </Field>
                      <Field label="Show">
                        <select
                          value={disposition}
                          onChange={(event) =>
                            setDisposition(event.target.value)
                          }
                        >
                          <option value="">All records</option>
                          <option value="accepted">Usable unique</option>
                          <option value="duplicate">Duplicates</option>
                          <option value="rejected">Malformed / excluded</option>
                        </select>
                      </Field>
                      <Field label="Provider">
                        <select
                          value={providerFilter}
                          onChange={(event) =>
                            setProviderFilter(event.target.value)
                          }
                        >
                          <option value="">All providers</option>
                          {providers.map((provider) => (
                            <option key={provider}>{provider}</option>
                          ))}
                        </select>
                      </Field>
                      <span className="filter-note">
                        {individualMode
                          ? "Row selection is exact; filters only change what is visible."
                          : "Choose rows to select individual records."}
                      </span>
                    </div>
                    <div className="table-wrap">
                      <table className="data-table">
                        <caption className="sr-only">
                          Address records in {job.source_filename}
                        </caption>
                        <thead>
                          <tr>
                            <th scope="col">Select</th>
                            <th scope="col">Address</th>
                            <th scope="col">Group</th>
                            <th scope="col">Visible domain</th>
                            <th scope="col">Host check</th>
                            <th scope="col">Record</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.length ? (
                            rows.map((row) => (
                              <tr
                                key={row.record_key}
                                className={
                                  selectedRows.has(row.record_key)
                                    ? "is-selected"
                                    : ""
                                }
                              >
                                <td>
                                  <input
                                    type="checkbox"
                                    aria-label={`Select ${row.email}`}
                                    checked={selectedRows.has(row.record_key)}
                                    disabled={!individualMode}
                                    onChange={() => toggleRow(row)}
                                  />
                                </td>
                                <td>
                                  <strong>
                                    {row.email || row.original_input}
                                  </strong>
                                  {row.original_input !== row.email && (
                                    <small>{row.original_input}</small>
                                  )}
                                </td>
                                <td>
                                  <span className="provider-label">
                                    {row.provider}
                                  </span>
                                </td>
                                <td>{row.domain || "—"}</td>
                                <td>
                                  <StatusTag value={row.mail_host_state} />
                                </td>
                                <td>
                                  <span className="row-status">
                                    {row.status}
                                  </span>
                                </td>
                              </tr>
                            ))
                          ) : (
                            <tr>
                              <td colSpan={6}>
                                <Empty
                                  title="No matching records"
                                  text="Change the filters or wait for the sorted result to finish."
                                />
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                    <div className="table-footer">
                      <span>
                        Showing {rows.length ? offset + 1 : 0}–
                        {Math.min(
                          offset + rows.length,
                          job.summary?.counts?.total || rows.length,
                        )}{" "}
                        of {job.summary?.counts?.total ?? rows.length} rows
                      </span>
                      <div className="button-group">
                        <button
                          disabled={offset === 0}
                          onClick={() =>
                            void loadRows(job.id, Math.max(0, offset - 100))
                          }
                        >
                          Previous
                        </button>
                        <button
                          disabled={!rows.length || rows.length < 100}
                          onClick={() => void loadRows(job.id, offset + 100)}
                        >
                          Next
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </>
            )}
          </Panel>
          <Panel className="result-footer">
            <PanelHeading
              eyebrow="SOURCE INTEGRITY"
              title="What the counts mean"
            />
            <div className="integrity-grid">
              <Metric
                label="Rows in file"
                value={job.summary?.counts?.total ?? job.processed_count ?? 0}
              />
              <Metric label="Usable unique" value={job.accepted_count ?? 0} />
              <Metric
                label="Duplicates"
                value={job.summary?.counts?.duplicates_removed ?? 0}
              />
              <Metric
                label="Malformed"
                value={job.summary?.counts?.invalid ?? 0}
              />
            </div>
            <p className="panel-detail">
              The visible table can be filtered without changing the current
              scope. Transfers use provider groups or exact record keys, never
              the visible rows by accident.
            </p>
          </Panel>
        </>
      )}
      {saveOpen && (
        <div className="dialog-backdrop" role="presentation">
          <div
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="save-title"
          >
            <h2 id="save-title">Save this exact selection</h2>
            <p>
              Only the current scope will be saved. Reopening it will not expand
              membership.
            </p>
            <Field label="List name">
              <input
                autoFocus
                value={saveName}
                onChange={(event) => setSaveName(event.target.value)}
                placeholder="For example: webinar invitees"
              />
            </Field>
            <div className="button-group dialog-actions">
              <button onClick={() => setSaveOpen(false)}>Cancel</button>
              <button
                className="primary-button"
                disabled={!saveName.trim()}
                onClick={saveSelection}
              >
                Save list
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function CompareLists({
  data,
  onRun,
  onNavigate,
}: {
  data: ApiRecord;
  onRun: Action;
  onNavigate: Navigate;
}) {
  const [lists, setLists] = useState<ApiRecord[]>([]);
  const [mainFile, setMainFile] = useState<File | null>(null);
  const [referenceFiles, setReferenceFiles] = useState<File[]>([]);
  const [mainList, setMainList] = useState(
    () => new URLSearchParams(window.location.search).get("list_id") || "",
  );
  const [references, setReferences] = useState<string[]>([]);
  const [history, setHistory] = useState<string[]>([]);
  const [operation, setOperation] = useState("subtract");
  const [result, setResult] = useState<ApiRecord | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    void api<ApiRecord>("/lists").then((value) => setLists(value.items || []));
  }, []);
  const rows: ApiRecord[] = result?.results || [];
  const sourceChanged = () => {
    setResult(null);
    setSelected(new Set());
  };
  const calculate = () =>
    onRun(async () => {
      if (!mainFile && !mainList)
        throw new Error("Choose a main list or import one");
      const value = await uploadComparison(mainFile, referenceFiles, {
        operation,
        main_list_id: mainList,
        reference_list_ids: JSON.stringify(references),
        campaign_ids: JSON.stringify(history),
      });
      setResult(value);
      setSelected(
        new Set((value.results || []).map((item: ApiRecord) => item.email)),
      );
      return value;
    }, "Comparison calculated; no source list was changed");
  const exportResult = () =>
    result &&
    onRun(async () => {
      const value =
        selected.size === rows.length
          ? result
          : {
              ...result,
              results: rows.filter((item) => selected.has(item.email)),
              counts: { ...result.counts, result: selected.size },
            };
      if (!selected.size)
        throw new Error("Choose at least one result before exporting");
      downloadBlob(await exportComparison(value), "mailforge-comparison.csv");
    }, "Comparison result exported");
  const sendResult = () =>
    result &&
    onRun(async () => {
      const chosen = rows.filter((item) => selected.has(item.email));
      if (!chosen.length)
        throw new Error("Choose at least one result before composing");
      const handoff = await api<ApiRecord>("/list-comparison/handoff", {
        method: "POST",
        body: JSON.stringify({ results: chosen, name: "Comparison result" }),
      });
      onNavigate(`send?list_id=${encodeURIComponent(handoff.list.id)}`);
      return handoff;
    }, "Exact comparison result is ready in Compose");
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">LIST COMPARISON</p>
          <h2>Put sources side by side.</h2>
          <p className="lede">
            The operation and sources are visible before you calculate. Changing
            either clears the previous result.
          </p>
        </div>
        <span className="context-badge">Read-only operation</span>
      </div>
      <Panel className="compare-input-panel">
        <PanelHeading
          eyebrow="1 / SOURCES"
          title="Choose a main list and any references"
          detail="Saved lists, local files and successful previous sends can be combined as references."
        />
        <div className="compare-source-grid">
          <div className="source-box">
            <strong>Main list</strong>
            <Field label="Saved list">
              <select
                value={mainList}
                onChange={(event) => {
                  setMainList(event.target.value);
                  setMainFile(null);
                  sourceChanged();
                }}
              >
                <option value="">Import a file instead…</option>
                {lists.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.contact_count ?? 0}
                  </option>
                ))}
              </select>
            </Field>
            <label className="file-picker compact">
              <span>{mainFile ? mainFile.name : "Choose main file"}</span>
              <input
                aria-label="Main comparison file"
                type="file"
                accept=".txt,.csv,.json,.jsonl"
                onChange={(event) => {
                  setMainFile(event.target.files?.[0] || null);
                  setMainList("");
                  sourceChanged();
                }}
              />
            </label>
          </div>
          <div className="source-box">
            <strong>Reference lists</strong>
            <div className="check-list">
              {lists.length ? (
                lists.map((item) => (
                  <label key={item.id}>
                    <input
                      type="checkbox"
                      checked={references.includes(item.id)}
                      onChange={(event) => {
                        setReferences((previous) =>
                          event.target.checked
                            ? [...previous, item.id]
                            : previous.filter((id) => id !== item.id),
                        );
                        sourceChanged();
                      }}
                    />
                    {item.name} <small>{item.contact_count ?? 0}</small>
                  </label>
                ))
              ) : (
                <span className="muted">No saved lists yet.</span>
              )}
            </div>
            <label className="file-picker compact">
              <span>
                {referenceFiles.length
                  ? `${referenceFiles.length} reference file${referenceFiles.length === 1 ? "" : "s"}`
                  : "Add reference files"}
              </span>
              <input
                aria-label="Reference comparison files"
                type="file"
                multiple
                accept=".txt,.csv,.json,.jsonl"
                onChange={(event) => {
                  setReferenceFiles(Array.from(event.target.files || []));
                  sourceChanged();
                }}
              />
            </label>
          </div>
          <div className="source-box history-box">
            <strong>Previous successful sends</strong>
            <div className="check-list">
              {(data.items || []).length ? (
                (data.items || []).map((item: ApiRecord) => (
                  <label key={item.id}>
                    <input
                      type="checkbox"
                      checked={history.includes(item.id)}
                      onChange={(event) => {
                        setHistory((previous) =>
                          event.target.checked
                            ? [...previous, item.id]
                            : previous.filter((id) => id !== item.id),
                        );
                        sourceChanged();
                      }}
                    />
                    {item.name} <small>{item.accepted ?? 0} accepted</small>
                  </label>
                ))
              ) : (
                <span className="muted">No successful sends are recorded.</span>
              )}
            </div>
          </div>
        </div>
        <div className="compare-action-row">
          <Field label="Operation">
            <select
              value={operation}
              onChange={(event) => {
                setOperation(event.target.value);
                sourceChanged();
              }}
            >
              <option value="subtract">Only in main</option>
              <option value="intersection">In both selections</option>
              <option value="union">Combined unique</option>
              <option value="unique_main">Unique to main</option>
              <option value="duplicates">Duplicates in main</option>
            </select>
          </Field>
          <div>
            <button className="primary-button" onClick={calculate}>
              Calculate result
            </button>
            <small>Changing the operation clears the previous result.</small>
          </div>
        </div>
      </Panel>
      {result ? (
        <Panel className="compare-result">
          <div className="result-toolbar">
            <div>
              <p className="eyebrow teal">2 / RESULT</p>
              <h3>
                {result.operation === "subtract"
                  ? "Only in main"
                  : result.operation}
              </h3>
              <p className="panel-detail">
                {selected.size} of {rows.length} result rows selected ·{" "}
                {result.reference_label || "No references"}
              </p>
            </div>
            <div className="button-group">
              <button onClick={exportResult} disabled={!selected.size}>
                Export selected
              </button>
              <button
                className="primary-button"
                onClick={sendResult}
                disabled={!selected.size}
              >
                Compose selected
              </button>
            </div>
          </div>
          <div className="result-summary">
            <Metric label="Result rows" value={rows.length} />
            <Metric
              label="Main unique"
              value={result.counts?.main_unique ?? 0}
            />
            <Metric
              label="Reference unique"
              value={result.counts?.reference_unique ?? 0}
            />
            <Metric
              label="Malformed main rows"
              value={result.counts?.main_invalid ?? 0}
            />
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <caption className="sr-only">Comparison result</caption>
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      aria-label="Select all comparison results"
                      checked={selected.size === rows.length && rows.length > 0}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? new Set(rows.map((row) => row.email))
                            : new Set(),
                        )
                      }
                    />
                  </th>
                  <th>Email address</th>
                  <th>Classification</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.email}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${row.email}`}
                        checked={selected.has(row.email)}
                        onChange={() =>
                          setSelected((previous) => {
                            const next = new Set(previous);
                            if (next.has(row.email)) next.delete(row.email);
                            else next.add(row.email);
                            return next;
                          })
                        }
                      />
                    </td>
                    <td>
                      <strong>{row.email}</strong>
                    </td>
                    <td>{row.classification}</td>
                    <td>{row.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : (
        <Panel className="empty-result">
          <Empty
            title="No result is active"
            text="Choose sources and calculate. A changed source or operation cannot leave a stale result actionable."
          />
        </Panel>
      )}
    </div>
  );
}

function Sender({
  onRun,
  onNavigate,
}: {
  onRun: Action;
  onNavigate: Navigate;
}) {
  const [lists, setLists] = useState<ApiRecord[]>([]);
  const [accounts, setAccounts] = useState<ApiRecord[]>([]);
  const [recipientText, setRecipientText] = useState("");
  const [recipientFile, setRecipientFile] = useState<File | null>(null);
  const [listId, setListId] = useState(
    new URLSearchParams(window.location.search).get("list_id") || "",
  );
  const [sender, setSender] = useState("sender@example.com");
  const [account, setAccount] = useState("");
  const [subject, setSubject] = useState("");
  const [text, setText] = useState("");
  const [html, setHtml] = useState("");
  const [htmlFile, setHtmlFile] = useState<File | null>(null);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [embeddedAssets, setEmbeddedAssets] = useState<File[]>([]);
  const [label, setLabel] = useState("");
  const [campaign, setCampaign] = useState<ApiRecord | null>(null);
  const [draftMeta, setDraftMeta] = useState({
    subject: "",
    text: "",
    sender: "",
  });
  const [preview, setPreview] = useState<ApiRecord | null>(null);
  const [testRecipient, setTestRecipient] = useState("");
  const [testPrepared, setTestPrepared] = useState("");
  useEffect(() => {
    void api<ApiRecord>("/lists").then((value) => setLists(value.items || []));
    void api<ApiRecord>("/providers").then((value) =>
      setAccounts(value.items || []),
    );
  }, []);
  useEffect(() => {
    if (!listId) return;
    const selected = lists.find((item) => item.id === listId);
    if (selected)
      setRecipientText(
        `${selected.name} · ${selected.contact_count ?? 0} saved addresses`,
      );
  }, [listId, lists]);
  useEffect(() => {
    if (!htmlFile) return;
    void htmlFile.text().then(setHtml);
  }, [htmlFile]);
  useEffect(() => {
    if (!campaign?.campaign?.id) return;
    const timer = window.setInterval(() => {
      void api<ApiRecord>(`/campaigns/${campaign.campaign.id}/workspace`).then(
        setCampaign,
      );
    }, 1200);
    return () => window.clearInterval(timer);
  }, [campaign?.campaign?.id]);
  const canReview = Boolean(
    (recipientText.trim() || listId || recipientFile) &&
    sender.trim() &&
    subject.trim() &&
    (text.trim() || html.trim()),
  );
  const create = () =>
    onRun(async () => {
      let resolvedListId = listId;
      const sourceFile =
        recipientFile ||
        (!listId && recipientText.trim()
          ? new File([recipientText], "pasted-addresses.txt", {
              type: "text/plain",
            })
          : null);
      if (!resolvedListId && sourceFile) {
        await upload("/imports/preview", sourceFile);
        const created = await api<ApiRecord>("/lists", {
          method: "POST",
          body: JSON.stringify({
            name: label.trim() || `Recipients ${Date.now()}`,
            description: "Recipient list prepared from Compose",
          }),
        });
        await upload(
          `/imports?list_id=${encodeURIComponent(created.id)}`,
          sourceFile,
        );
        resolvedListId = created.id;
        setListId(created.id);
      }
      if (!resolvedListId)
        throw new Error("Choose or import at least one recipient");
      const attachmentRefs: ApiRecord[] = [];
      for (const item of attachments) {
        const asset = await uploadAsset(item, "attachment");
        attachmentRefs.push({ asset_id: asset.id });
      }
      const assetManifest: ApiRecord[] = [];
      for (const item of embeddedAssets) {
        const asset = await uploadAsset(item, "image");
        assetManifest.push({ asset_id: asset.id, filename: item.name });
      }
      const document = {
        version: 1,
        name: subject,
        subject,
        preheader: "",
        fallbacks: {},
        attachments: attachmentRefs,
        ...(html.trim()
          ? {
              composition_mode: "custom_html",
              custom_html: {
                original_source: html.trim(),
                asset_manifest: assetManifest,
              },
              blocks: [{ type: "text", text: text || "Message" }],
            }
          : { blocks: [{ type: "text", text }] }),
      };
      const createdDoc = await api<ApiRecord>("/message-studio/documents", {
        method: "POST",
        body: JSON.stringify({
          name: subject || "Mailforge message",
          kind: "message",
          document,
        }),
      });
      const published = await api<ApiRecord>(
        `/message-studio/documents/${createdDoc.id}/publish`,
        { method: "POST", body: JSON.stringify({}) },
      );
      const revisionId =
        published.current_published_revision_id ||
        createdDoc.current_published_revision_id;
      const created = await api<ApiRecord>("/campaigns", {
        method: "POST",
        body: JSON.stringify({
          name: label.trim() || `Send · ${subject}`,
          list_id: resolvedListId,
          message_revision_id: revisionId,
          provider_account_ref: account || null,
          sender_email: sender,
          subject,
          text,
        }),
      });
      const workspace = await api<ApiRecord>(
        `/campaigns/${created.id}/workspace`,
      );
      setDraftMeta({ subject, text, sender });
      setCampaign(workspace);
      return workspace;
    }, "Draft ready; review the exact audience before any send");
  const action = (path: string, message: string) =>
    campaign?.campaign?.id &&
    onRun(async () => {
      let result: ApiRecord;
      if (path === "preview")
        result = await api<ApiRecord>(
          `/campaigns/${campaign.campaign.id}/preview`,
        );
      else
        result = await api<ApiRecord>(
          `/campaigns/${campaign.campaign.id}/${path}`,
          {
            method: "POST",
            ...(path === "execute"
              ? { body: JSON.stringify({ mode: "loopback_smtp" }) }
              : {}),
          },
        );
      if (path === "preview") setPreview(result);
      const next = await api<ApiRecord>(
        `/campaigns/${campaign.campaign.id}/workspace`,
      );
      setCampaign(next);
      return result;
    }, message);
  const schedule = (scheduledAt: string) =>
    campaign?.campaign?.id &&
    onRun(async () => {
      const result = await api<ApiRecord>(
        `/campaigns/${campaign.campaign.id}/schedule`,
        {
          method: "PUT",
          body: JSON.stringify({
            mode: "at",
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
            scheduled_at: new Date(scheduledAt).toISOString(),
            weekdays: [0, 1, 2, 3, 4, 5, 6],
          }),
        },
      );
      setCampaign(
        await api<ApiRecord>(`/campaigns/${campaign.campaign.id}/workspace`),
      );
      return result;
    }, "Operation scheduled locally");
  const localTest = () => {
    if (testRecipient.trim()) setTestPrepared(testRecipient.trim());
  };
  if (campaign)
    return (
      <SendReview
        campaign={campaign}
        draftMeta={draftMeta}
        account={account}
        testRecipient={testRecipient}
        setTestRecipient={setTestRecipient}
        testPrepared={testPrepared}
        preview={preview}
        onAction={action}
        onSchedule={schedule}
        onNew={() => {
          setCampaign(null);
          setPreview(null);
          setTestPrepared("");
        }}
        onNavigate={onNavigate}
        onLocalTest={localTest}
      />
    );
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">COMPOSE</p>
          <h2>Build the message around its real inputs.</h2>
          <p className="lede">
            Send is independent. Start with pasted addresses, a file, a saved
            list or a result transferred from another tool.
          </p>
        </div>
        <span className="context-badge">Review before sending</span>
      </div>
      <div className="compose-grid">
        <Panel>
          <PanelHeading
            eyebrow="1 / RECIPIENTS"
            title="Who should receive it?"
            detail="Large audiences are summarized; inspect the saved list when you need the full set."
          />
          <Field label="Saved list">
            <select
              value={listId}
              onChange={(event) => setListId(event.target.value)}
            >
              <option value="">Choose a saved list…</option>
              {lists.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.contact_count ?? 0} addresses
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Paste addresses"
            hint="One address per line, or paste a CSV column."
          >
            <textarea
              rows={7}
              value={listId ? "" : recipientText}
              disabled={Boolean(listId)}
              onChange={(event) => setRecipientText(event.target.value)}
              placeholder="person@example.com\nanother@example.net"
            />
          </Field>
          <label className="file-picker">
            <span>
              {recipientFile ? recipientFile.name : "Import a recipient file"}
            </span>
            <input
              aria-label="Recipient file"
              type="file"
              accept=".txt,.csv,.json,.jsonl"
              onChange={(event) => {
                setRecipientFile(event.target.files?.[0] || null);
                setListId("");
              }}
            />
          </label>
          {listId && (
            <div className="selected-source">
              <strong>
                {lists.find((item) => item.id === listId)?.name || "Saved list"}
              </strong>
              <span>
                {lists.find((item) => item.id === listId)?.contact_count ??
                  "Saved"}{" "}
                addresses · exact membership retained
              </span>
              <button className="text-link" onClick={() => setListId("")}>
                Choose another source
              </button>
            </div>
          )}
        </Panel>
        <Panel>
          <PanelHeading
            eyebrow="2 / MESSAGE"
            title="Write or import the message"
            detail="Plain text is optional when HTML is supplied. Personalization is rendered by the message service."
          />
          <Field label="Sender email">
            <input
              value={sender}
              onChange={(event) => setSender(event.target.value)}
            />
          </Field>
          <Field label="Subject">
            <input
              value={subject}
              onChange={(event) => setSubject(event.target.value)}
              placeholder="A clear subject"
            />
          </Field>
          <Field label="Plain-text version">
            <textarea
              rows={5}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="The readable fallback for every recipient"
            />
          </Field>
          <Field
            label="HTML message"
            hint="Paste existing HTML, or import it directly from a file."
          >
            <textarea
              rows={8}
              value={html}
              onChange={(event) => setHtml(event.target.value)}
              placeholder="<html>…</html>"
            />
          </Field>
          <label className="file-picker">
            <span>{htmlFile ? htmlFile.name : "Import HTML file"}</span>
            <input
              aria-label="HTML file"
              type="file"
              accept=".html,.htm"
              onChange={(event) => setHtmlFile(event.target.files?.[0] || null)}
            />
          </label>
          {html && (
            <div className="html-preview">
              <span>HTML preview</span>
              <iframe
                title="HTML message preview"
                srcDoc={html}
                sandbox="allow-same-origin"
              />
            </div>
          )}
        </Panel>
        <Panel>
          <PanelHeading
            eyebrow="3 / OPTIONS"
            title="Delivery details"
            detail="Account selection and optional files live here without interrupting composition."
          />
          <Field
            label="Email account"
            hint="Required by the final send, not by sorting or comparison."
          >
            <select
              value={account}
              onChange={(event) => setAccount(event.target.value)}
            >
              <option value="">Choose when ready</option>
              {accounts.map((item) => (
                <option key={item.ref} value={item.ref}>
                  {item.display_name || item.ref} ·{" "}
                  {item.sender_email || "no sender"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Operation label">
            <input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="Optional note for Activity"
            />
          </Field>
          <label className="file-picker">
            <span>
              {attachments.length
                ? `${attachments.length} attachment${attachments.length === 1 ? "" : "s"}`
                : "Add attachments"}
            </span>
            <input
              aria-label="Message attachments"
              type="file"
              multiple
              onChange={(event) =>
                setAttachments(Array.from(event.target.files || []))
              }
            />
          </label>
          <label className="file-picker">
            <span>
              {embeddedAssets.length
                ? `${embeddedAssets.length} embedded asset${embeddedAssets.length === 1 ? "" : "s"}`
                : "Add embedded images or CSS assets"}
            </span>
            <input
              aria-label="Embedded message assets"
              type="file"
              multiple
              accept="image/*,.css"
              onChange={(event) =>
                setEmbeddedAssets(Array.from(event.target.files || []))
              }
            />
          </label>
          <details className="advanced-details">
            <summary>Optional personalization and scheduling</summary>
            <p>
              Use <code>{"{{ email }}"}</code> and structured fields in the
              message. Scheduling remains available after the exact scope is
              prepared.
            </p>
          </details>
          <button
            className="primary-button full-button"
            disabled={!canReview}
            onClick={create}
          >
            Review exact send
          </button>
        </Panel>
      </div>
    </div>
  );
}

function SendReview({
  campaign,
  draftMeta,
  account,
  testRecipient,
  setTestRecipient,
  testPrepared,
  preview,
  onAction,
  onSchedule,
  onNew,
  onNavigate,
  onLocalTest,
}: {
  campaign: ApiRecord;
  draftMeta: { subject: string; text: string; sender: string };
  account: string;
  testRecipient: string;
  setTestRecipient: (value: string) => void;
  testPrepared: string;
  preview: ApiRecord | null;
  onAction: (path: string, message: string) => void;
  onSchedule: (scheduledAt: string) => void;
  onNew: () => void;
  onNavigate: Navigate;
  onLocalTest: () => void;
}) {
  const status = campaign.campaign?.status || "draft";
  const [scheduleAt, setScheduleAt] = useState("");
  const report = campaign.report || {};
  const recipients: ApiRecord[] =
    campaign.recipients || campaign.audience?.items || [];
  const total =
    campaign.preflight?.audience_summary?.matched ??
    recipients.length ??
    report.total ??
    0;
  const messageSubject =
    campaign.campaign?.configuration?.subject ||
    draftMeta.subject ||
    "Untitled message";
  const messageText =
    campaign.campaign?.configuration?.text ||
    draftMeta.text ||
    "HTML message attached to this revision.";
  const sender =
    campaign.campaign?.configuration?.sender_email ||
    draftMeta.sender ||
    "Sender selected in message";
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">COMPOSE / REVIEW</p>
          <h2>{campaign.campaign?.name || "Review this send"}</h2>
          <p className="lede">
            The operation is not sent from this screen until you explicitly
            authorize and start it.
          </p>
        </div>
        <StatusTag value={status === "draft" ? "Draft" : status} />
      </div>
      <Panel className="review-panel">
        <div className="scope-strip review-scope">
          <div>
            <span className="eyebrow">EXACT RECIPIENT SCOPE</span>
            <strong>{total.toLocaleString()} recipients</strong>
            <small>{sender}</small>
          </div>
          <div className="button-group">
            <button onClick={() => onNavigate("saved")}>
              Inspect saved list
            </button>
            <button onClick={() => onNew()}>Start another</button>
          </div>
        </div>
        <div className="review-grid">
          <div>
            <h3>Message preview</h3>
            <div className="message-card">
              <strong>{messageSubject}</strong>
              <p>{messageText}</p>
            </div>
            {preview?.intents?.length ? (
              <div className="preview-box">
                <strong>Prepared recipients</strong>
                <span>
                  {preview.intents.length} intents · scope hash retained by the
                  service
                </span>
              </div>
            ) : (
              <button
                onClick={() =>
                  onAction("preview", "Prepared recipient preview loaded")
                }
              >
                Inspect prepared recipients
              </button>
            )}
          </div>
          <div>
            <h3>Operation controls</h3>
            <ol className="review-steps">
              <li
                className={
                  ["ready", "authorized", "running", "completed"].includes(
                    status,
                  )
                    ? "done"
                    : ""
                }
              >
                <strong>Prepare</strong>
                <span>
                  Evaluate the saved audience and preserve the exact revision.
                </span>
              </li>
              <li
                className={
                  ["authorized", "running", "completed"].includes(status)
                    ? "done"
                    : ""
                }
              >
                <strong>Authorize local simulation</strong>
                <span>
                  Name the mode before any controlled local execution.
                </span>
              </li>
              <li className={status === "completed" ? "done" : ""}>
                <strong>Run</strong>
                <span>
                  Provider acceptance is recorded separately from inbox
                  delivery.
                </span>
              </li>
            </ol>
            <div className="button-group wrap-controls">
              {status === "draft" && (
                <button
                  className="primary-button"
                  onClick={() =>
                    onAction(
                      "prepare",
                      "Audience prepared; review the exact count",
                    )
                  }
                >
                  Prepare exact scope
                </button>
              )}
              {["ready", "prepared", "paused"].includes(status) && (
                <button
                  className="primary-button"
                  onClick={() =>
                    onAction("authorize", "Local simulation authorized")
                  }
                >
                  Authorize simulation
                </button>
              )}
              {["authorized", "ready"].includes(status) && (
                <button
                  onClick={() =>
                    onAction("execute", "Controlled local operation started")
                  }
                >
                  Start local simulation
                </button>
              )}
              {status === "running" && (
                <button onClick={() => onAction("pause", "Operation paused")}>
                  Pause
                </button>
              )}
              {status === "paused" && (
                <button onClick={() => onAction("resume", "Operation resumed")}>
                  Resume
                </button>
              )}
              {["running", "paused", "ready"].includes(status) && (
                <button
                  className="danger-button"
                  onClick={() => onAction("cancel", "Operation canceled")}
                >
                  Cancel
                </button>
              )}
            </div>
            {["ready", "authorized"].includes(status) && (
              <div className="schedule-control">
                <Field
                  label="Schedule (optional)"
                  hint="Uses your local timezone."
                >
                  <input
                    type="datetime-local"
                    value={scheduleAt}
                    onChange={(event) => setScheduleAt(event.target.value)}
                  />
                </Field>
                <button
                  onClick={() => onSchedule(scheduleAt)}
                  disabled={!scheduleAt}
                >
                  Schedule exact send
                </button>
              </div>
            )}
          </div>
        </div>
      </Panel>
      <Panel className="test-panel">
        <PanelHeading
          eyebrow="SEPARATE TEST"
          title="Test one recipient"
          detail="This is independent from the prepared audience. It does not expand or change the final recipient scope."
        />
        <div className="test-row">
          <Field label="Test recipient">
            <input
              value={testRecipient}
              onChange={(event) => setTestRecipient(event.target.value)}
              placeholder="you@example.com"
            />
          </Field>
          <button
            onClick={onLocalTest}
            disabled={!testRecipient.trim() || !account}
          >
            Prepare test operation
          </button>
        </div>
        {testPrepared && (
          <div className="preview-box">
            <strong>One-recipient test prepared</strong>
            <span>{testPrepared} · account selected · audience unchanged</span>
            <small>
              Provider execution remains behind the canonical authorization
              boundary and was not started here.
            </small>
          </div>
        )}
        <p className="panel-detail">
          A provider test remains behind the canonical authorization and
          credential boundaries. No external operation is started by this
          review.
        </p>
      </Panel>
    </div>
  );
}

function Activity({
  data,
  onNavigate,
}: {
  data: ApiRecord;
  onNavigate: Navigate;
}) {
  const [items, setItems] = useState<ApiRecord[]>(
    data.items || data.campaigns || [],
  );
  useEffect(() => {
    void Promise.all([
      api<ApiRecord>("/campaigns"),
      api<ApiRecord>("/list-tools/jobs"),
      api<ApiRecord>("/list-comparison/history"),
    ]).then(([campaigns, jobs, comparisons]) => {
      const sendItems = (campaigns.items || []).map((item: ApiRecord) => ({
        ...item,
        activityType: "Send",
        activityCount: item.report?.counts?.accepted ?? 0,
      }));
      const sortItems = (jobs.items || []).map((item: ApiRecord) => ({
        ...item,
        name: item.source_filename,
        activityType: "Sort",
        activityCount: item.accepted_count ?? 0,
      }));
      const compareItems = (comparisons.items || []).map((item: ApiRecord) => ({
        ...item,
        activityType: "Compare",
        activityCount: item.accepted ?? item.prepared ?? 0,
      }));
      setItems(
        [...sendItems, ...sortItems, ...compareItems].sort((a, b) =>
          String(b.updated_at || b.created_at).localeCompare(
            String(a.updated_at || a.created_at),
          ),
        ),
      );
    });
  }, []);
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">ACTIVITY</p>
          <h2>Automatic records, when they help.</h2>
          <p className="lede">
            Activity is supporting context for recovery and review, not another
            work queue.
          </p>
        </div>
      </div>
      <Panel className="table-panel">
        {items.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Object</th>
                <th>Type</th>
                <th>Status</th>
                <th>Count</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.activityType}-${item.id}`}>
                  <td>
                    <strong>
                      {item.name || item.source_filename || "Unnamed operation"}
                    </strong>
                    <small>
                      {item.configuration?.sender_email ||
                        item.profile ||
                        "Local record"}
                    </small>
                  </td>
                  <td>{item.activityType || "Send"}</td>
                  <td>
                    <StatusTag value={item.status || "Recorded"} />
                  </td>
                  <td>{item.activityCount ?? 0}</td>
                  <td>
                    {item.updated_at
                      ? new Date(item.updated_at).toLocaleString()
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="Nothing recorded yet"
            text="Prepared and controlled sending operations will appear here."
            action="Open Compose"
            onAction={() => onNavigate("send")}
          />
        )}
      </Panel>
    </div>
  );
}
function Accounts({ data, onRun }: { data: ApiRecord; onRun: Action }) {
  const [ref, setRef] = useState("local-account");
  const [display, setDisplay] = useState("My email account");
  const [sender, setSender] = useState("sender@example.com");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [tls, setTls] = useState("starttls");
  const accounts: ApiRecord[] = data.items || [];
  const save = () =>
    onRun(async () => {
      const result = await api<ApiRecord>("/providers", {
        method: "POST",
        body: JSON.stringify({
          ref,
          display_name: display,
          sender_email: sender,
          kind: "smtp",
          host,
          port: Number(port),
          username,
          credential_ref: `keychain:${ref}`,
          tls_mode: tls,
          auth_method: "password",
          pacing_per_minute: 60,
          max_concurrency: 1,
        }),
      });
      if (password)
        await api("/providers/credentials", {
          method: "POST",
          body: JSON.stringify({
            provider_ref: ref,
            credential_ref: `keychain:${ref}`,
            secret: password,
          }),
        });
      setPassword("");
      return result;
    }, "Account saved locally; the password was not returned");
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">ACCOUNTS</p>
          <h2>Add an account only when sending requires it.</h2>
          <p className="lede">
            Credentials remain behind the local credential boundary and are
            never shown in ordinary diagnostics.
          </p>
        </div>
      </div>
      <div className="two-column">
        <Panel>
          <PanelHeading
            title="Account details"
            detail="These settings are independent from file sorting and comparison."
          />
          <Field label="Account name">
            <input
              value={display}
              onChange={(event) => setDisplay(event.target.value)}
            />
          </Field>
          <Field label="Sender email">
            <input
              value={sender}
              onChange={(event) => setSender(event.target.value)}
            />
          </Field>
          <Field label="SMTP server">
            <input
              value={host}
              onChange={(event) => setHost(event.target.value)}
              placeholder="smtp.example.com"
            />
          </Field>
          <div className="field-pair">
            <Field label="Port">
              <input
                inputMode="numeric"
                value={port}
                onChange={(event) => setPort(event.target.value)}
              />
            </Field>
            <Field label="Encryption">
              <select
                value={tls}
                onChange={(event) => setTls(event.target.value)}
              >
                <option value="starttls">STARTTLS</option>
                <option value="implicit_tls">TLS</option>
              </select>
            </Field>
          </div>
          <Field label="Username">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </Field>
          <Field label="Password">
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <details className="advanced-details">
            <summary>Advanced local reference</summary>
            <Field label="Reference">
              <input
                value={ref}
                onChange={(event) => setRef(event.target.value)}
              />
            </Field>
          </details>
          <button className="primary-button full-button" onClick={save}>
            Save account
          </button>
        </Panel>
        <Panel>
          <PanelHeading
            title="Saved accounts"
            detail="Mailforge shows capability and status, never secrets."
          />
          {accounts.length ? (
            <div className="saved-stack">
              {accounts.map((item) => (
                <div className="saved-row" key={item.ref}>
                  <span>
                    <strong>{item.display_name || item.ref}</strong>
                    <small>
                      {item.sender_email || "No sender"} ·{" "}
                      {item.host || "Host not set"}:{item.port || "—"}
                    </small>
                  </span>
                  <StatusTag
                    value={item.outbound_enabled ? "Ready" : "Needs review"}
                  />
                </div>
              ))}
            </div>
          ) : (
            <Empty
              title="No accounts yet"
              text="You can sort and compare without configuring an account."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}
function SavedLists({
  data,
  onNavigate,
}: {
  data: ApiRecord;
  onNavigate: Navigate;
}) {
  const items: ApiRecord[] = data.items || [];
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">SAVED LISTS</p>
          <h2>Reusable address lists.</h2>
          <p className="lede">
            Membership is durable and exact. These lists are optional
            conveniences, not administrative work items.
          </p>
        </div>
      </div>
      <Panel className="table-panel">
        {items.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Addresses</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <strong>{item.name}</strong>
                    <small>{item.description}</small>
                  </td>
                  <td>{item.contact_count ?? 0}</td>
                  <td>
                    {item.created_at
                      ? new Date(item.created_at).toLocaleDateString()
                      : "—"}
                  </td>
                  <td>
                    <button
                      onClick={() =>
                        onNavigate(
                          `send?list_id=${encodeURIComponent(item.id)}`,
                        )
                      }
                    >
                      Compose
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="No saved lists yet"
            text="Save an exact scope from Addresses or transfer a comparison result."
            action="Open Addresses"
            onAction={() => onNavigate("sort")}
          />
        )}
      </Panel>
    </div>
  );
}
function Settings({ data, onRun }: { data: ApiRecord; onRun: Action }) {
  const [backups, setBackups] = useState<ApiRecord[]>([]);
  useEffect(() => {
    void api<ApiRecord>("/runtime/backups").then((value) =>
      setBackups(value.items || []),
    );
  }, []);
  const createBackup = () =>
    onRun(async () => {
      const result = await api<ApiRecord>("/runtime/backup", {
        method: "POST",
      });
      setBackups((previous) => [result.metadata, ...previous]);
      return result;
    }, "Local backup created");
  const verifyBackup = (name: string) =>
    onRun(async () => {
      const result = await api<ApiRecord>(
        `/runtime/backups/${encodeURIComponent(name)}/verify`,
        { method: "POST" },
      );
      setBackups((previous) =>
        previous.map((item) =>
          item.name === name
            ? { ...item, ...result.metadata, valid: result.valid }
            : item,
        ),
      );
      return result;
    }, "Backup verified");
  return (
    <div className="workspace">
      <div className="workspace-head">
        <div>
          <p className="eyebrow teal">SETTINGS</p>
          <h2>Local workspace details.</h2>
          <p className="lede">
            Technical records are available when needed without interrupting
            ordinary list work.
          </p>
        </div>
      </div>
      <div className="two-column">
        <Panel>
          <PanelHeading title="Runtime" />
          <dl className="detail-list">
            <div>
              <dt>Application</dt>
              <dd>{data.version || "Mailforge"}</dd>
            </div>
            <div>
              <dt>Storage</dt>
              <dd>Local SQLite</dd>
            </div>
            <div>
              <dt>Network</dt>
              <dd>Loopback interface</dd>
            </div>
            <div>
              <dt>Database integrity</dt>
              <dd>{data.database_integrity || "Not checked"}</dd>
            </div>
            <div>
              <dt>Delivery claim</dt>
              <dd>Provider acceptance only</dd>
            </div>
          </dl>
        </Panel>
        <Panel>
          <PanelHeading
            title="Backup and recovery"
            detail="Backups use the existing local authority; no file paths or credentials are exposed."
          />
          <div className="button-group">
            <button className="primary-button" onClick={createBackup}>
              Create local backup
            </button>
          </div>
          {backups.length ? (
            <div className="saved-stack backup-list">
              {backups.map((item) => (
                <div className="saved-row" key={item.name}>
                  <span>
                    <strong>{item.name}</strong>
                    <small>
                      {item.valid === false
                        ? "Needs review"
                        : "Available locally"}
                    </small>
                  </span>
                  <button onClick={() => verifyBackup(item.name)}>
                    Verify
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="panel-detail">
              No backup has been recorded in this workspace.
            </p>
          )}
          <p className="panel-detail">
            Prepared scopes, authorization evidence, progress and recovery
            records remain available to the local services. External SMTP stays
            separately gated.
          </p>
        </Panel>
      </div>
    </div>
  );
}

export default App;
