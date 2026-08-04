"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  HistoryEntry,
  HistoryResponse,
  OkResult,
  QueryResult,
  QueryUiState,
  SchemaResponse,
  SchemaTable,
} from "../lib/api-types";
import SqlEditor, { type SqlEditorHandle } from "./SqlEditor";
import SchemaBrowser from "./SchemaBrowser";
import ExampleList from "./ExampleList";
import HistoryPanel from "./HistoryPanel";
import ResultsArea from "./ResultsArea";

const SQL_STORAGE_KEY = "bball-oracle.sandbox.sql";
const DEFAULT_SQL = "SELECT * FROM nba.pbp_event LIMIT 20";

function failureFrom(status: number, body: Record<string, unknown>): QueryResult {
  const error = typeof body.error === "string" ? body.error : `request failed (${status})`;
  if (status === 429) {
    const retryAfterSeconds =
      typeof body.retryAfterSeconds === "number" ? body.retryAfterSeconds : 60;
    return { status: "rate_limited", error, retryAfterSeconds };
  }
  if (status === 504) {
    const durationMs = typeof body.durationMs === "number" ? body.durationMs : 0;
    return { status: "timeout", error, durationMs };
  }
  // Both validator rejections and SQL errors are 400s; only executed queries
  // carry durationMs, which is what tells them apart.
  if (typeof body.durationMs === "number") {
    return { status: "error", error, durationMs: body.durationMs };
  }
  return { status: "validation_rejected", error };
}

export default function Sandbox() {
  // Lazy initializer instead of a load-on-mount effect: window is absent during SSR,
  // where the sandbox renders no editor content anyway (CSR by design).
  const [sql, setSql] = useState(() =>
    typeof window === "undefined"
      ? DEFAULT_SQL
      : (localStorage.getItem(SQL_STORAGE_KEY) ?? DEFAULT_SQL),
  );
  const [schema, setSchema] = useState<SchemaTable[]>([]);
  const [uiState, setUiState] = useState<QueryUiState>("idle");
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [retryRemaining, setRetryRemaining] = useState(0);
  const editorRef = useRef<SqlEditorHandle>(null);

  useEffect(() => {
    localStorage.setItem(SQL_STORAGE_KEY, sql);
  }, [sql]);

  const refreshHistory = useCallback(async () => {
    try {
      const res = await fetch("/api/history");
      if (res.ok) setHistory(((await res.json()) as HistoryResponse).queries);
    } catch {
      // history is auxiliary; a failed refresh keeps the last known list
    }
  }, []);

  useEffect(() => {
    fetch("/api/schema")
      .then(async (res) => {
        if (res.ok) setSchema(((await res.json()) as SchemaResponse).tables);
      })
      .catch(() => {});
    Promise.resolve().then(refreshHistory);
  }, [refreshHistory]);

  useEffect(() => {
    if (retryRemaining <= 0) return;
    const timer = setTimeout(() => setRetryRemaining((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [retryRemaining]);

  const running = uiState === "running";
  const runDisabled = running || retryRemaining > 0 || sql.trim() === "";

  const run = useCallback(async () => {
    if (running || retryRemaining > 0 || sql.trim() === "") return;
    setUiState("running");
    let result: QueryResult;
    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ sql }),
      });
      if (res.status === 401) {
        window.location.href = "/signin";
        return;
      }
      const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      if (res.ok) {
        result = body as unknown as OkResult;
      } else {
        result = failureFrom(res.status, body);
        if (result.status === "rate_limited") setRetryRemaining(result.retryAfterSeconds);
      }
    } catch (err) {
      result = {
        status: "error",
        error: `network error: ${err instanceof Error ? err.message : String(err)}`,
        durationMs: 0,
      };
    }
    setUiState(result);
    refreshHistory();
  }, [running, retryRemaining, sql, refreshHistory]);

  return (
    <div className="grid min-h-0 flex-1 grid-cols-[15rem_minmax(0,1fr)_19rem] gap-3 p-3">
      <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto">
        <SchemaBrowser
          schema={schema}
          onInsert={(name) => editorRef.current?.insertAtCursor(name)}
        />
        <ExampleList onSelect={setSql} />
      </aside>

      <section className="flex min-h-0 flex-col gap-2">
        <div className="h-64 shrink-0">
          <SqlEditor
            ref={editorRef}
            value={sql}
            onChange={setSql}
            onRun={run}
            disabled={runDisabled}
            schema={schema}
          />
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={run}
            disabled={runDisabled}
            className="rounded-panel bg-accent px-4 py-1.5 text-sm font-semibold text-accent-ink hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running ? "Running…" : "Run"}
          </button>
          <span className="text-xs text-ink-faint">⌘⏎ / Ctrl+Enter</span>
          {retryRemaining > 0 && (
            <span className="text-xs text-warning">
              Rate limited — retry in {retryRemaining}s
            </span>
          )}
        </div>
        <ResultsArea state={uiState} />
      </section>

      <aside className="min-h-0 overflow-y-auto">
        <HistoryPanel entries={history} onSelect={setSql} />
      </aside>
    </div>
  );
}
