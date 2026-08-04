"use client";

import type { HistoryEntry, HistoryStatus } from "../lib/api-types";

const STATUS_BADGES: Record<HistoryStatus, { label: string; classes: string }> = {
  ok: { label: "ok", classes: "border-ok/40 text-ok" },
  validation_rejected: { label: "rejected", classes: "border-warning/40 text-warning" },
  timeout: { label: "timeout", classes: "border-danger/40 text-danger" },
  error: { label: "error", classes: "border-danger/40 text-danger" },
};

type Props = {
  entries: HistoryEntry[] | null;
  onSelect: (sql: string) => void;
};

export default function HistoryPanel({ entries, onSelect }: Props) {
  return (
    <div className="flex flex-col gap-1">
      <h2 className="px-1 text-xs font-semibold uppercase tracking-wide text-ink-faint">
        History
      </h2>
      {entries === null && <p className="px-1 text-xs text-ink-faint">Loading…</p>}
      {entries?.length === 0 && (
        <p className="px-1 text-xs text-ink-faint">No queries yet.</p>
      )}
      <ul className="flex flex-col gap-1">
        {entries?.map((entry, i) => {
          const badge = STATUS_BADGES[entry.status] ?? STATUS_BADGES.error;
          return (
            <li key={`${entry.startedAt}-${i}`}>
              <button
                type="button"
                onClick={() => onSelect(entry.sql)}
                className="w-full rounded-panel border border-edge px-2 py-1.5 text-left hover:bg-surface-raised"
              >
                <span className="flex items-center justify-between gap-2">
                  <span
                    className={`rounded-panel border px-1.5 text-[10px] font-semibold ${badge.classes}`}
                  >
                    {badge.label}
                  </span>
                  <span className="text-[10px] text-ink-faint">
                    {new Date(entry.startedAt).toLocaleTimeString()}
                    {entry.durationMs !== null && ` · ${entry.durationMs}ms`}
                    {entry.rowCount !== null && ` · ${entry.rowCount} rows`}
                  </span>
                </span>
                <span className="mt-1 block truncate font-mono text-xs text-ink-muted">
                  {entry.sql.replace(/\s+/g, " ").trim()}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
