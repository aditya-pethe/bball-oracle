"use client";

import type { QueryUiState } from "../lib/api-types";
import TableView from "./TableView";

function Notice({
  tone,
  title,
  children,
}: {
  tone: "danger" | "warning";
  title: string;
  children: React.ReactNode;
}) {
  const toneClasses =
    tone === "danger"
      ? "border-danger/40 bg-danger/10 text-danger"
      : "border-warning/40 bg-warning/10 text-warning";
  return (
    <div className={`rounded-panel border px-4 py-3 text-sm ${toneClasses}`}>
      <p className="font-semibold">{title}</p>
      <p className="mt-1 whitespace-pre-wrap font-mono text-xs">{children}</p>
    </div>
  );
}

export default function ResultsArea({ state }: { state: QueryUiState }) {
  if (state === "idle") {
    return (
      <p className="px-1 py-4 text-sm text-ink-faint">
        Run a query to see results.
      </p>
    );
  }
  if (state === "running") {
    return (
      <p className="animate-pulse px-1 py-4 text-sm text-ink-muted">Running…</p>
    );
  }

  switch (state.status) {
    case "ok":
      return (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <div className="flex items-baseline gap-3 px-1 text-xs text-ink-muted">
            <span>
              {state.rowCount.toLocaleString()} row{state.rowCount === 1 ? "" : "s"}
            </span>
            <span>{state.durationMs.toLocaleString()} ms</span>
          </div>
          {state.truncated && (
            <p className="rounded-panel border border-warning/40 bg-warning/10 px-3 py-1.5 text-xs text-warning">
              Results truncated — showing the first {state.rowCount.toLocaleString()} rows.
            </p>
          )}
          <TableView result={state} />
        </div>
      );
    case "validation_rejected":
      return (
        <Notice tone="warning" title="Query rejected">
          {state.error}
        </Notice>
      );
    case "error":
      return (
        <Notice tone="danger" title="SQL error">
          {state.error}
        </Notice>
      );
    case "timeout":
      return (
        <Notice tone="danger" title="Query timed out">
          {state.error}
        </Notice>
      );
    case "rate_limited":
      return (
        <Notice tone="warning" title="Rate limited">
          {`${state.error}\nRetry in ${state.retryAfterSeconds}s.`}
        </Notice>
      );
  }
}
