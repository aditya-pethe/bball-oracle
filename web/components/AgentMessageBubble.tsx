"use client";

import type { AgentOutcome } from "../lib/api-types";
import { NODE_LABELS } from "../lib/agent-client";
import type { AgentTurn } from "./AgentTab";
import ResultsArea from "./ResultsArea";

type Props = {
  turn: Extract<AgentTurn, { role: "assistant" }>;
  question: string;
  onOpenInEditor: (sql: string) => void;
  onRetry: (question: string) => void;
};

/**
 * `answer`/`clarify`/`decline` render distinctly (.agents/p4_agent.md "UI (per the mock)").
 * A decline is styled as information, not failure — it gets the same neutral treatment as
 * clarify, never the danger tone reserved for the stream-level error state below.
 */
const OUTCOME_STYLES: Record<AgentOutcome, { label: string; classes: string }> = {
  answer: { label: "Answer", classes: "border-edge text-ink-muted" },
  clarify: { label: "Needs clarification", classes: "border-accent/40 text-accent" },
  decline: { label: "Can't answer this", classes: "border-ink-faint/40 text-ink-muted" },
};

export default function AgentMessageBubble({ turn, question, onOpenInEditor, onRetry }: Props) {
  // A local const (not a repeated `turn.envelope` property access) so TypeScript's narrowing
  // survives into the closures below (onClick handlers) instead of needing `!` everywhere.
  const envelope = turn.status === "done" ? turn.envelope : null;

  return (
    <div className="max-w-[85%] rounded-panel border border-edge bg-surface-raised/40 px-3 py-2">
      {turn.status === "streaming" && (
        <ol className="flex flex-col gap-0.5 text-xs text-ink-muted">
          {turn.nodeProgress.length === 0 && <li className="animate-pulse">Thinking…</li>}
          {turn.nodeProgress.map((p) => (
            <li key={p.node} className="flex items-center gap-2">
              <span className="text-ok">✓</span>
              <span>{NODE_LABELS[p.node] ?? p.node}</span>
              {p.durationMs !== null && (
                <span className="text-ink-faint">{Math.round(p.durationMs)}ms</span>
              )}
            </li>
          ))}
        </ol>
      )}

      {turn.status === "error" && (
        <div className="rounded-panel border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          <p className="font-semibold">Something went wrong</p>
          <p className="mt-1 text-xs">{turn.error}</p>
          <button
            type="button"
            onClick={() => onRetry(question)}
            className="mt-2 rounded-panel border border-danger/40 px-2 py-1 text-xs font-semibold hover:bg-danger/10"
          >
            Try again
          </button>
        </div>
      )}

      {envelope && (
        <div className="flex flex-col gap-2">
          <span
            className={`w-fit rounded-panel border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${OUTCOME_STYLES[envelope.outcome].classes}`}
          >
            {OUTCOME_STYLES[envelope.outcome].label}
          </span>

          {envelope.summary && (
            <p className="whitespace-pre-wrap text-sm text-ink">{envelope.summary}</p>
          )}

          {/* A stored turn can carry an error with no result to hang it on — an interrupted
              stream, or a correction ladder that exhausted its budget. Before this, such a turn
              rendered as an outcome badge and nothing else: the explanation was persisted and
              then never shown, which is how a timeout came to look like the agent answering
              with silence. `result` is the other renderer for the same field (agent-client.ts's
              toQueryResult folds a wire error into a QueryResult), so this only fires when that
              one cannot — hence the guard, which keeps SQL errors from being reported twice. */}
          {envelope.error && !envelope.result && (
            <p className="rounded-panel border border-danger/40 bg-danger/5 px-2 py-1 text-xs text-danger">
              {envelope.error}
            </p>
          )}

          {/* Design principle 1 (.agents/p4_agent.md): every answer discloses the SQL that
              produced it, collapsed by default but always present, one click from the editor —
              never hidden, never gated behind a hover. */}
          {envelope.sql && (
            <details className="rounded-panel border border-edge/60">
              <summary className="cursor-pointer select-none px-2 py-1 text-xs font-semibold text-ink-muted hover:text-ink">
                SQL used
              </summary>
              <div className="border-t border-edge/60 p-2">
                <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs text-ink-muted">
                  {envelope.sql}
                </pre>
                <button
                  type="button"
                  onClick={() => onOpenInEditor(envelope.sql as string)}
                  className="mt-2 rounded-panel border border-edge px-2 py-1 text-xs font-semibold text-ink-muted hover:bg-surface-raised hover:text-ink"
                >
                  Open in SQL editor
                </button>
              </div>
            </details>
          )}

          {/* Reuses ResultsArea/TableView as-is (.agents/p4_agent.md "UI (per the mock)") —
              the payoff of Phase 2's props-in/callbacks-out architecture. This also covers an
              `answer` whose SQL never produced usable rows: agent-client.ts's toQueryResult
              maps that case into the same {status:"error"} shape /api/query returns, so it
              renders through ResultsArea's existing "SQL error" notice rather than a second,
              bespoke error renderer. */}
          {envelope.result && (
            <div className="flex max-h-80 min-h-0 flex-col">
              <ResultsArea state={envelope.result} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
