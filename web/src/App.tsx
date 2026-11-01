import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AuditEvent,
  type Corrections,
  type Matter,
  type QueueItem,
  type ResolveBody,
  type ReviewDetail,
} from "./api";

// The API's default page. This page does not offer paging, so it says when there may be more.
const QUEUE_PAGE_SIZE = 50;

const DOCUMENT_TYPES = [
  "COMPLAINT",
  "SUMMONS",
  "SUBPOENA",
  "MEDICAL_RECORD",
  "BILLING_STATEMENT",
  "CORRESPONDENCE",
  "DISCOVERY_REQUEST",
  "EFILING_NOTICE",
  "OTHER",
];

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "That API key was not accepted.";
    if (error.status === 409) {
      return `${error.message}. Reload the document before trying again.`;
    }
    return error.message;
  }
  return "The API could not be reached.";
}

function SignIn({ onSubmit }: { onSubmit: (key: string) => void }) {
  const [value, setValue] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (value.trim()) onSubmit(value.trim());
  };
  return (
    <form className="signin" onSubmit={submit}>
      <h1>Mailroom review</h1>
      <label>
        API key
        <input
          type="password"
          autoComplete="off"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
      </label>
      <p className="hint">Kept in this tab's memory only. `make seed` prints a key.</p>
      <button type="submit" disabled={!value.trim()}>
        Open queue
      </button>
    </form>
  );
}

function Fields({ fields }: { fields: Record<string, unknown> }) {
  const entries = Object.entries(fields).filter(
    ([, value]) => value !== null && !(Array.isArray(value) && value.length === 0),
  );
  if (entries.length === 0) return <p className="muted">No fields were extracted.</p>;
  return (
    <dl>
      {entries.map(([name, value]) => (
        <div key={name}>
          <dt>{name.replaceAll("_", " ")}</dt>
          <dd>{Array.isArray(value) ? value.join(", ") : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function History({ events }: { events: AuditEvent[] }) {
  return (
    <ol className="history">
      {events.map((event, index) => (
        <li key={index}>
          <strong>{event.reason}</strong>
          <span className="muted">
            {" "}
            {event.to_state} · {event.actor} · {new Date(event.created_at).toLocaleTimeString()}
          </span>
        </li>
      ))}
    </ol>
  );
}

function MatterSearch({
  apiKey,
  onChoose,
  disabled,
}: {
  apiKey: string;
  onChoose: (matter: Matter) => void;
  disabled: boolean;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Matter[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const search = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      setResults(await api.matters(apiKey, query.trim()));
    } catch (caught) {
      setError(errorText(caught));
    }
  };

  return (
    <div>
      <form className="row" onSubmit={search}>
        <input
          aria-label="Search matters"
          placeholder="Search by client or matter number"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="submit" className="secondary" disabled={!query.trim()}>
          Search
        </button>
      </form>
      {error && <p role="alert" className="error">{error}</p>}
      {results?.length === 0 && <p className="muted">No matters found.</p>}
      <ul className="plain">
        {results?.map((matter) => (
          <li key={matter.id} className="row spread">
            <span>
              {matter.matter_number} · {matter.client_name}
              {matter.status === "CLOSED" && <span className="tag">closed</span>}
            </span>
            <button
              type="button"
              className="secondary"
              disabled={disabled || matter.status === "CLOSED"}
              onClick={() => onChoose(matter)}
            >
              Assign
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Detail({
  apiKey,
  detail,
  onResolved,
  onReload,
}: {
  apiKey: string;
  detail: ReviewDetail;
  onResolved: (message: string, documentId: string) => void;
  onReload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [documentType, setDocumentType] = useState("");
  const [caseNumber, setCaseNumber] = useState("");
  const [documentDate, setDocumentDate] = useState("");

  const submit = async (body: Omit<ResolveBody, "expected_version" | "corrections">) => {
    const corrections: Corrections = {};
    if (documentType) corrections.document_type = documentType;
    if (caseNumber.trim()) corrections.case_number = caseNumber.trim();
    if (documentDate) corrections.document_date = documentDate;

    setBusy(true);
    setError(null);
    setConflict(false);
    try {
      const result = await api.resolve(apiKey, detail.document_id, {
        ...body,
        expected_version: detail.version,
        ...(Object.keys(corrections).length > 0 ? { corrections } : {}),
      });
      onResolved(`Saved. The document is now ${result.state}.`, detail.document_id);
    } catch (caught) {
      setError(errorText(caught));
      setConflict(caught instanceof ApiError && caught.status === 409);
    } finally {
      setBusy(false);
    }
  };

  const top = detail.candidates[0];
  return (
    <article>
      <header>
        <h2>{detail.original_filename}</h2>
        <p>
          <span className="tag">{detail.state}</span>
          <span className="tag">{detail.reason}</span>
          <span className="muted">version {detail.version}</span>
        </p>
      </header>

      {error && (
        <p role="alert" className="error">
          {error}{" "}
          {conflict && (
            <button type="button" className="link" onClick={onReload}>
              Reload
            </button>
          )}
        </p>
      )}

      <section>
        <h3>Extracted fields</h3>
        <Fields fields={detail.fields} />
      </section>

      <section>
        <h3>Classification</h3>
        {detail.classification ? (
          <p>
            {detail.classification.document_type} · confidence {detail.classification.confidence} ·{" "}
            {detail.classification.method}
          </p>
        ) : (
          <p className="muted">Not classified, because no text could be read.</p>
        )}
      </section>

      <section>
        <h3>Candidate matters</h3>
        {detail.candidates.length === 0 && <p className="muted">No candidates were found.</p>}
        <ol className="plain">
          {detail.candidates.map((candidate) => (
            <li key={candidate.matter_id} className="candidate">
              <div className="row spread">
                <strong>
                  {candidate.matter_number} · {candidate.client_name}
                  {candidate.closed && <span className="tag">closed</span>}
                </strong>
                <span>score {candidate.score}</span>
              </div>
              <p className="muted">{candidate.explanation}</p>
              <button
                type="button"
                className="secondary"
                disabled={busy || candidate.closed}
                onClick={() => submit({ action: "ASSIGN", matter_id: candidate.matter_id })}
              >
                Assign this matter
              </button>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h3>Find another matter</h3>
        <MatterSearch
          apiKey={apiKey}
          disabled={busy}
          onChoose={(matter) => submit({ action: "ASSIGN", matter_id: matter.id })}
        />
      </section>

      <section>
        <h3>Corrections</h3>
        <p className="muted">Applied with whichever action you take below.</p>
        <div className="grid">
          <label>
            Document type
            <select
              value={documentType}
              onChange={(event) => setDocumentType(event.target.value)}
            >
              <option value="">No change</option>
              {DOCUMENT_TYPES.map((type) => (
                <option key={type}>{type}</option>
              ))}
            </select>
          </label>
          <label>
            Case number
            <input
              value={caseNumber}
              maxLength={64}
              onChange={(event) => setCaseNumber(event.target.value)}
            />
          </label>
          <label>
            Document date
            <input
              type="date"
              value={documentDate}
              onChange={(event) => setDocumentDate(event.target.value)}
            />
          </label>
        </div>
      </section>

      <div className="actions">
        <button
          type="button"
          disabled={busy || !top || top.closed}
          onClick={() => submit({ action: "APPROVE" })}
        >
          {top ? `Approve ${top.matter_number}` : "Approve"}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy || detail.state === "UNMATCHED"}
          onClick={() => submit({ action: "MARK_UNMATCHED" })}
        >
          Mark unmatched
        </button>
        {busy && <span className="muted">Saving…</span>}
      </div>

      <section>
        <h3>Audit trail</h3>
        <History events={detail.events} />
      </section>
    </article>
  );
}

export function App() {
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lastTrail, setLastTrail] = useState<AuditEvent[] | null>(null);

  const loadQueue = useCallback(async (key: string) => {
    setQueueError(null);
    try {
      setQueue(await api.queue(key));
    } catch (caught) {
      setQueue(null);
      setQueueError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 401) setApiKey(null);
    }
  }, []);

  const loadDetail = useCallback(async (key: string, id: string) => {
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      setDetail(await api.detail(key, id));
    } catch (caught) {
      setDetailError(errorText(caught));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (apiKey) void loadQueue(apiKey);
  }, [apiKey, loadQueue]);

  if (!apiKey) {
    return (
      <main className="narrow">
        {queueError && <p role="alert" className="error">{queueError}</p>}
        <SignIn onSubmit={setApiKey} />
      </main>
    );
  }

  const choose = (id: string) => {
    setSelected(id);
    setNotice(null);
    setLastTrail(null);
    void loadDetail(apiKey, id);
  };

  const resolved = async (message: string, documentId: string) => {
    setNotice(message);
    setSelected(null);
    setDetail(null);
    await loadQueue(apiKey);
    try {
      setLastTrail(await api.events(apiKey, documentId));
    } catch {
      setLastTrail(null);
    }
  };

  return (
    <main>
      <aside>
        <div className="row spread">
          <h1>Review queue</h1>
          <button type="button" className="secondary" onClick={() => void loadQueue(apiKey)}>
            Refresh
          </button>
        </div>
        {queueError && <p role="alert" className="error">{queueError}</p>}
        {queue === null && !queueError && <p className="muted">Loading…</p>}
        {queue?.length === 0 && <p className="muted">Nothing is waiting for review.</p>}
        {queue?.length === QUEUE_PAGE_SIZE && (
          <p className="muted">Showing the oldest {QUEUE_PAGE_SIZE}. Resolve some to see more.</p>
        )}
        <ul className="queue">
          {queue?.map((item) => (
            <li key={item.document_id}>
              <button
                type="button"
                aria-current={item.document_id === selected}
                onClick={() => choose(item.document_id)}
              >
                <strong>{item.original_filename}</strong>
                <span className="muted">
                  {item.state} · {item.reason}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="panel">
        {notice && <p role="status" className="success">{notice}</p>}
        {detailLoading && <p className="muted">Loading document…</p>}
        {detailError && <p role="alert" className="error">{detailError}</p>}
        {detail && (
          <Detail
            key={`${detail.document_id}:${detail.version}`}
            apiKey={apiKey}
            detail={detail}
            onResolved={(message, id) => void resolved(message, id)}
            onReload={() => void loadDetail(apiKey, detail.document_id)}
          />
        )}
        {lastTrail && (
          <section>
            <h3>Audit trail so far</h3>
            <History events={lastTrail} />
          </section>
        )}
        {!detail && !detailLoading && !detailError && !notice && (
          <p className="muted">Choose a document from the queue.</p>
        )}
      </section>
    </main>
  );
}
