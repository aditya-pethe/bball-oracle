"use client";

import type { AgentOutcome } from "../lib/api-types";
import type { AgentConversation } from "./AgentTab";

const TAG_STYLES: Record<AgentOutcome, string> = {
  answer: "border-ok/40 text-ok",
  clarify: "border-accent/40 text-accent",
  decline: "border-ink-faint/40 text-ink-muted",
};

function lastOutcome(conversation: AgentConversation): AgentOutcome | null {
  for (let i = conversation.turns.length - 1; i >= 0; i--) {
    const turn = conversation.turns[i];
    if (turn.role === "assistant" && turn.envelope) return turn.envelope.outcome;
  }
  return null;
}

type Props = {
  conversations: AgentConversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
};

export default function AgentConversationRail({ conversations, activeId, onSelect, onNew }: Props) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between px-1">
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
      <ul className="flex flex-col gap-1">
        {conversations.map((c) => {
          const outcome = lastOutcome(c);
          return (
            <li key={c.id}>
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
                <span className="flex items-center justify-between gap-2">
                  {outcome && (
                    <span
                      className={`rounded-panel border px-1.5 text-[10px] font-semibold ${TAG_STYLES[outcome]}`}
                    >
                      {outcome}
                    </span>
                  )}
                  <span className="ml-auto text-[10px] text-ink-faint">
                    {new Date(c.createdAt).toLocaleTimeString()}
                  </span>
                </span>
                <span className="mt-1 block truncate text-xs text-ink-muted">
                  {c.title ?? "New conversation"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
