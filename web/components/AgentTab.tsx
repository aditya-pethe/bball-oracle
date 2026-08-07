"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentEnvelope, SchemaTable } from "../lib/api-types";
import { AgentClientError, streamAgentAnswer } from "../lib/agent-client";
import SchemaBrowser from "./SchemaBrowser";
import AgentConversationRail from "./AgentConversationRail";
import AgentMessageThread from "./AgentMessageThread";
import AgentComposer from "./AgentComposer";

/**
 * Agent tab state, kept local to this component (`useState`, no persistence).
 *
 * Migration 0004 (`app.conversation` / `app.conversation_message`) is written but not yet
 * applied to any live database (AGENTS.md Status), and this step's file ownership does not
 * include a conversation-list API route — so "conversations" here are session-local threads
 * that reset on refresh, not the persisted kind the schema anticipates. The shapes below are
 * deliberately close to `Conversation`/`AgentMessage` in api-types.ts so wiring real
 * persistence later is a fetch-and-hydrate, not a redesign.
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

export type AgentConversation = {
  id: string;
  title: string | null;
  createdAt: number;
  updatedAt: number;
  turns: AgentTurn[];
};

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function truncateTitle(text: string): string {
  const trimmed = text.trim().replace(/\s+/g, " ");
  return trimmed.length > 60 ? `${trimmed.slice(0, 57)}…` : trimmed;
}

type Props = {
  schema: SchemaTable[];
  onOpenInEditor: (sql: string) => void;
};

export default function AgentTab({ schema, onOpenInEditor }: Props) {
  const [conversations, setConversations] = useState<AgentConversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const mountedRef = useRef(true);

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const active = conversations.find((c) => c.id === activeId) ?? null;
  const streaming =
    active?.turns.some((t) => t.role === "assistant" && t.status === "streaming") ?? false;

  const newConversation = useCallback(() => {
    const conv: AgentConversation = {
      id: newId(),
      title: null,
      createdAt: Date.now(),
      updatedAt: Date.now(),
      turns: [],
    };
    setConversations((prev) => [conv, ...prev]);
    setActiveId(conv.id);
  }, []);

  const updateConversation = useCallback(
    (id: string, fn: (c: AgentConversation) => AgentConversation) => {
      setConversations((prev) => prev.map((c) => (c.id === id ? fn(c) : c)));
    },
    [],
  );

  const updateAssistantTurn = useCallback(
    (
      convId: string,
      turnId: string,
      fn: (t: Extract<AgentTurn, { role: "assistant" }>) => Extract<AgentTurn, { role: "assistant" }>,
    ) => {
      updateConversation(convId, (c) => ({
        ...c,
        turns: c.turns.map((t) => (t.role === "assistant" && t.id === turnId ? fn(t) : t)),
        updatedAt: Date.now(),
      }));
    },
    [updateConversation],
  );

  const ask = useCallback(
    async (question: string) => {
      let convId = activeId;
      if (!convId) {
        const conv: AgentConversation = {
          id: newId(),
          title: truncateTitle(question),
          createdAt: Date.now(),
          updatedAt: Date.now(),
          turns: [],
        };
        setConversations((prev) => [conv, ...prev]);
        setActiveId(conv.id);
        convId = conv.id;
      }

      const userTurn: AgentTurn = { role: "user", id: newId(), text: question, createdAt: Date.now() };
      const assistantId = newId();
      const assistantTurn: AgentTurn = {
        role: "assistant",
        id: assistantId,
        createdAt: Date.now(),
        status: "streaming",
        nodeProgress: [],
        envelope: null,
        error: null,
      };

      updateConversation(convId, (c) => ({
        ...c,
        title: c.title ?? truncateTitle(question),
        turns: [...c.turns, userTurn, assistantTurn],
        updatedAt: Date.now(),
      }));

      try {
        for await (const event of streamAgentAnswer({ question, conversationId: convId })) {
          if (!mountedRef.current) return;
          if (event.type === "node") {
            updateAssistantTurn(convId, assistantId, (t) => {
              const idx = t.nodeProgress.findIndex((p) => p.node === event.node);
              const nodeProgress = [...t.nodeProgress];
              const entry = { node: event.node, durationMs: event.durationMs };
              if (idx === -1) nodeProgress.push(entry);
              else nodeProgress[idx] = entry;
              return { ...t, nodeProgress };
            });
          } else {
            updateAssistantTurn(convId, assistantId, (t) => ({
              ...t,
              status: "done",
              envelope: event.envelope,
            }));
          }
        }
      } catch (err) {
        if (!mountedRef.current) return;
        if (err instanceof AgentClientError && err.status === 401) {
          window.location.href = "/signin";
          return;
        }
        const message = err instanceof Error ? err.message : String(err);
        updateAssistantTurn(convId, assistantId, (t) => ({ ...t, status: "error", error: message }));
      }
    },
    [activeId, updateAssistantTurn, updateConversation],
  );

  const submit = useCallback(
    (question: string) => {
      setDraft("");
      void ask(question);
    },
    [ask],
  );

  return (
    <div className="grid min-h-0 flex-1 grid-cols-[15rem_minmax(0,1fr)] gap-3 p-3">
      <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto">
        <AgentConversationRail
          conversations={conversations}
          activeId={activeId}
          onSelect={setActiveId}
          onNew={newConversation}
        />
        <SchemaBrowser
          schema={schema}
          onInsert={(name) => setDraft((prev) => (prev ? `${prev} ${name}` : name))}
        />
      </aside>

      <section className="flex min-h-0 flex-col gap-2">
        <AgentMessageThread turns={active?.turns ?? []} onOpenInEditor={onOpenInEditor} onRetry={submit} />
        <AgentComposer value={draft} onChange={setDraft} onSubmit={submit} disabled={streaming} />
      </section>
    </div>
  );
}
