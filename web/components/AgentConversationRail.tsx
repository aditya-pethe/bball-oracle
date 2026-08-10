"use client";

import type { Conversation } from "../lib/api-types";

/**
 * The persisted thread list (`GET /api/conversations`). Threads outlive the browser tab now,
 * so the rail shows the stored `updatedAt` rather than a session-local timestamp, and the
 * outcome tag the session-local version carried is gone — the list endpoint deliberately does
 * not read messages, which is what keeps it a single indexed query.
 */
type Props = {
  conversations: Conversation[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
};

function when(iso: string): string {
  const date = new Date(iso);
  const sameDay = new Date().toDateString() === date.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function AgentConversationRail({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  return (
    <div className="flex min-h-0 flex-col gap-1">
      <div className="flex shrink-0 items-center justify-between px-1">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-faint">
          Conversations
        </h2>
        <button
          type="button"
          onClick={onNew}
          className="rounded-panel px-1.5 py-0.5 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
        >
          + New
        </button>
      </div>
      {conversations.length === 0 && (
        <p className="px-1 text-xs text-ink-faint">No conversations yet — ask a question below.</p>
      )}
      <ul className="flex min-h-0 flex-col gap-1 overflow-y-auto">
        {conversations.map((c) => (
          <li key={c.id} className="group relative">
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={c.id === activeId}
              className={`w-full rounded-panel border px-2 py-1.5 text-left ${
                c.id === activeId
                  ? "border-accent/50 bg-surface-raised"
                  : "border-edge hover:bg-surface-raised"
              }`}
            >
              <span className="block truncate pr-5 text-xs text-ink-muted">
                {c.title ?? "New conversation"}
              </span>
              <span className="mt-0.5 block text-[10px] text-ink-faint">{when(c.updatedAt)}</span>
            </button>
            <button
              type="button"
              aria-label={`Delete conversation ${c.title ?? c.id}`}
              onClick={() => onDelete(c.id)}
              className="absolute right-1 top-1 rounded-panel px-1 text-xs text-ink-faint opacity-0 hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
