"use client";

import { useEffect, useRef } from "react";
import type { AgentTurn } from "./AgentTab";
import AgentMessageBubble from "./AgentMessageBubble";

type Props = {
  turns: AgentTurn[];
  onOpenInEditor: (sql: string) => void;
  onRetry: (question: string) => void;
};

/** The nearest user turn before index `i` — what a retry after an assistant failure resends. */
function precedingQuestion(turns: AgentTurn[], i: number): string {
  for (let j = i - 1; j >= 0; j--) {
    const turn = turns[j];
    if (turn.role === "user") return turn.text;
  }
  return "";
}

export default function AgentMessageThread({ turns, onOpenInEditor, onRetry }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  if (turns.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <p className="max-w-sm text-center text-sm text-ink-faint">
          Ask a question in natural language — the agent drafts SQL, runs it through the same
          safety chain as the editor, and shows its work.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">
      {turns.map((turn, i) =>
        turn.role === "user" ? (
          <div
            key={turn.id}
            className="ml-auto max-w-[75%] rounded-panel bg-accent/10 px-3 py-2 text-sm text-ink"
          >
            {turn.text}
          </div>
        ) : (
          <AgentMessageBubble
            key={turn.id}
            turn={turn}
            question={precedingQuestion(turns, i)}
            onOpenInEditor={onOpenInEditor}
            onRetry={onRetry}
          />
        ),
      )}
      <div ref={bottomRef} />
    </div>
  );
}
