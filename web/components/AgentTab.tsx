"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentEnvelope,
  AgentStatusResponse,
  Conversation,
  ConversationListResponse,
  ConversationResponse,
  SchemaTable,
} from "../lib/api-types";
import { AgentClientError, streamAgentAnswer } from "../lib/agent-client";
import SchemaBrowser from "./SchemaBrowser";
import AgentConversationRail from "./AgentConversationRail";
import AgentMessageThread from "./AgentMessageThread";
import AgentComposer from "./AgentComposer";

/**
 * Conversations are persisted server-side (`app.conversation` / `app.conversation_message`),
 * so this component owns *rendering* state only: the thread list, which thread is open, and
 * the turns of that thread. A question is written to the database by `/api/agent` itself —
 * the user turn and the assistant envelope are stored around the proxy call, which is what
 * makes `query_log.conversation_message_id` point at something real.
 *
 * `AgentTurn` remains the render model rather than `AgentMessage` because a turn in flight has
 * state a stored row does not: per-node progress, and a stream-level failure that is not an
 * envelope at all. Persisted messages are hydrated into it on load.
 */
export type AgentTurn =
  | { role: "user"; id: string; text: string; createdAt: number }
  | {
      role: "assistant";
      id: string;
      createdAt: number;
      status: "streaming" | "done" | "error";
      nodeProgress: { node: string; durationMs: number | null }[];
      envelope: AgentEnvelope | null;
      /** A stream/network failure (service down, dropped connection) — distinct from an
       * envelope whose `outcome` is a normal "answer" that happens to carry an error. */
      error: string | null;
    };

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function hydrate(response: ConversationResponse): AgentTurn[] {
  return response.messages.map((message) =>
    message.role === "user"
      ? {
          role: "user" as const,
          id: String(message.id),
          text: message.text,
          createdAt: new Date(message.createdAt).getTime(),
        }
      : {
          role: "assistant" as const,
          id: String(message.id),
          createdAt: new Date(message.createdAt).getTime(),
          status: "done" as const,
          nodeProgress: [],
          envelope: message.envelope,
          error: null,
        },
  );
}

type Props = {
  schema: SchemaTable[];
  onOpenInEditor: (sql: string) => void;
};

export default function AgentTab({ schema, onOpenInEditor }: Props) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [turns, setTurns] = useState<AgentTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState<AgentStatusResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const refreshConversations = useCallback(async () => {
    try {
      const res = await fetch("/api/conversations");
      if (!res.ok || !mountedRef.current) return;
      setConversations(((await res.json()) as ConversationListResponse).conversations);
    } catch {
      // the rail is auxiliary; a failed refresh keeps the last known list
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/agent");
      if (!res.ok || !mountedRef.current) return;
      setStatus((await res.json()) as AgentStatusResponse);
    } catch {
      // unknown status is not a broken tab: the composer stays usable and a failed ask
      // surfaces its own message.
    }
  }, []);

  useEffect(() => {
    // Deferred off the effect body for the same reason Sandbox.tsx defers refreshHistory:
    // these resolve into setState, and calling setState synchronously inside an effect is a
    // cascading render (react-hooks/set-state-in-effect).
    Promise.resolve().then(refreshConversations);
    Promise.resolve().then(refreshStatus);
  }, [refreshConversations, refreshStatus]);

  const openConversation = useCallback(async (id: number) => {
    setActiveId(id);
    setTurns([]);
    setNotice(null);
    try {
      const res = await fetch(`/api/conversations/${id}`);
      if (!res.ok || !mountedRef.current) return;
      setTurns(hydrate((await res.json()) as ConversationResponse));
    } catch {
      if (mountedRef.current) setNotice("Could not load that conversation.");
    }
  }, []);

  const newConversation = useCallback(() => {
    // No row is created here: `/api/agent` creates the thread with the first question, so an
    // abandoned "+ New" leaves nothing behind.
    setActiveId(null);
    setTurns([]);
    setNotice(null);
  }, []);

  const removeConversation = useCallback(
    async (id: number) => {
      try {
        const res = await fetch(`/api/conversations/${id}`, { method: "DELETE" });
        if (!res.ok || !mountedRef.current) return;
        setConversations((prev) => prev.filter((c) => c.id !== id));
        setActiveId((prev) => (prev === id ? null : prev));
        setTurns((prev) => (activeId === id ? [] : prev));
      } catch {
        if (mountedRef.current) setNotice("Could not delete that conversation.");
      }
    },
    [activeId],
  );

  const updateAssistantTurn = useCallback(
    (
      turnId: string,
      fn: (t: Extract<AgentTurn, { role: "assistant" }>) => Extract<AgentTurn, { role: "assistant" }>,
    ) => {
      setTurns((prev) =>
        prev.map((t) => (t.role === "assistant" && t.id === turnId ? fn(t) : t)),
      );
    },
    [],
  );

  const ask = useCallback(
    async (question: string) => {
      const assistantId = newId();
      setNotice(null);
      setStreaming(true);
      setTurns((prev) => [
        ...prev,
        { role: "user", id: newId(), text: question, createdAt: Date.now() },
        {
          role: "assistant",
          id: assistantId,
          createdAt: Date.now(),
          status: "streaming",
          nodeProgress: [],
          envelope: null,
          error: null,
        },
      ]);

      let answered = false;
      try {
        for await (const event of streamAgentAnswer({ question, conversationId: activeId })) {
          if (!mountedRef.current) return;
          if (event.type === "meta") {
            if (event.conversationId !== null) setActiveId(event.conversationId);
            void refreshConversations();
          } else if (event.type === "node") {
            updateAssistantTurn(assistantId, (t) => {
              const idx = t.nodeProgress.findIndex((p) => p.node === event.node);
              const nodeProgress = [...t.nodeProgress];
              const entry = { node: event.node, durationMs: event.durationMs };
              if (idx === -1) nodeProgress.push(entry);
              else nodeProgress[idx] = entry;
              return { ...t, nodeProgress };
            });
          } else {
            answered = true;
            updateAssistantTurn(assistantId, (t) => ({
              ...t,
              status: "done",
              envelope: event.envelope,
            }));
          }
        }

        // The stream ended cleanly but never delivered a `done` event — the route hit its
        // 60s ceiling, or the service died mid-turn. Without this the turn sits on
        // `status: "streaming"` forever, showing a node checklist and no answer, which is
        // the second half of the 2026-08-10 report's symptom. The server has already
        // recorded the turn as interrupted; say the same thing here and offer the retry.
        if (!answered && mountedRef.current) {
          updateAssistantTurn(assistantId, (t) => ({
            ...t,
            status: "error",
            error: "the agent stopped before finishing this answer — it may have taken too long",
          }));
        }
      } catch (err) {
        if (!mountedRef.current) return;
        if (err instanceof AgentClientError && err.status === 401) {
          window.location.href = "/signin";
          return;
        }
        const message = err instanceof Error ? err.message : String(err);
        // A kill switch or an exhausted budget is a state of the tab, not a failed turn:
        // drop the optimistic pair and say so once, rather than leaving a dead bubble.
        if (err instanceof AgentClientError && (err.status === 503 || err.status === 429)) {
          setTurns((prev) => prev.slice(0, -2));
          setNotice(message);
          void refreshStatus();
        } else {
          updateAssistantTurn(assistantId, (t) => ({ ...t, status: "error", error: message }));
        }
      } finally {
        if (mountedRef.current) {
          setStreaming(false);
          void refreshStatus();
        }
      }
    },
    [activeId, refreshConversations, refreshStatus, updateAssistantTurn],
  );

  const submit = useCallback(
    (question: string) => {
      setDraft("");
      void ask(question);
    },
    [ask],
  );

  const disabled = status !== null && (!status.enabled || status.remaining <= 0);
  const disabledNotice = !status
    ? null
    : !status.enabled
      ? "The agent is currently disabled."
      : status.remaining <= 0
        ? `Daily agent limit reached (${status.used}/${status.limit} messages). It resets at midnight UTC.`
        : null;

  return (
    <div className="grid min-h-0 flex-1 grid-cols-[15rem_minmax(0,1fr)_19rem] grid-rows-[minmax(0,1fr)] gap-3 overflow-hidden p-3">
      <aside className="flex min-h-0 flex-col overflow-hidden">
        <AgentConversationRail
          conversations={conversations}
          activeId={activeId}
          onSelect={openConversation}
          onNew={newConversation}
          onDelete={removeConversation}
        />
      </aside>

      <section className="flex min-h-0 flex-col gap-2">
        <AgentMessageThread turns={turns} onOpenInEditor={onOpenInEditor} onRetry={submit} />
        {(notice ?? disabledNotice) && (
          <p className="shrink-0 rounded-panel border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            {notice ?? disabledNotice}
          </p>
        )}
        <div className="shrink-0">
          <AgentComposer
            value={draft}
            onChange={setDraft}
            onSubmit={submit}
            disabled={streaming || disabled}
            busy={streaming}
          />
        </div>
      </section>

      <aside className="min-h-0 overflow-y-auto">
        <SchemaBrowser
          schema={schema}
          onInsert={(name) => setDraft((prev) => (prev ? `${prev} ${name}` : name))}
        />
      </aside>
    </div>
  );
}
