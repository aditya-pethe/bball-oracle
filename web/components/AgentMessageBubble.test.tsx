// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import AgentMessageBubble from "./AgentMessageBubble";
import type { AgentTurn } from "./AgentTab";

afterEach(cleanup);

function doneTurn(overrides: Partial<Extract<AgentTurn, { role: "assistant" }>> = {}): Extract<
  AgentTurn,
  { role: "assistant" }
> {
  return {
    role: "assistant",
    id: "t1",
    createdAt: Date.now(),
    status: "done",
    nodeProgress: [],
    error: null,
    envelope: {
      outcome: "answer",
      summary: "705 shots were attempted.",
      sql: "SELECT COUNT(*) FROM nba.shot_detail",
      result: {
        status: "ok",
        columns: ["n"],
        rows: [[705]],
        rowCount: 1,
        truncated: false,
        durationMs: 120,
      },
      error: null,
    },
    ...overrides,
  };
}

describe("AgentMessageBubble — SQL disclosure (design principle 1)", () => {
  it("discloses the SQL collapsed by default, never hidden", () => {
    render(
      <AgentMessageBubble turn={doneTurn()} question="q" onOpenInEditor={vi.fn()} onRetry={vi.fn()} />,
    );

    const details = screen.getByText("SQL used").closest("details");
    expect(details).toBeTruthy();
    expect(details?.hasAttribute("open")).toBe(false);
    // The SQL text is present in the DOM even while collapsed -- "collapsed", not "absent".
    expect(screen.getByText("SELECT COUNT(*) FROM nba.shot_detail")).toBeTruthy();
  });

  it("'Open in SQL editor' is one click and hands back the exact SQL", () => {
    const onOpenInEditor = vi.fn();
    render(
      <AgentMessageBubble turn={doneTurn()} question="q" onOpenInEditor={onOpenInEditor} onRetry={vi.fn()} />,
    );

    fireEvent.click(screen.getByText("Open in SQL editor"));
    expect(onOpenInEditor).toHaveBeenCalledWith("SELECT COUNT(*) FROM nba.shot_detail");
  });

  it("renders results via the reused ResultsArea/TableView, not a second table", () => {
    render(
      <AgentMessageBubble turn={doneTurn()} question="q" onOpenInEditor={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByText("705")).toBeTruthy();
    expect(screen.getByText("1 row")).toBeTruthy();
  });

  it("an answer with no usable result still renders (mapped through ResultsArea's error notice)", () => {
    render(
      <AgentMessageBubble
        turn={doneTurn({
          envelope: {
            outcome: "answer",
            summary: null as unknown as string,
            sql: "SELECT * FROM nba.shot_detail WHERE 1=0",
            result: { status: "error", error: "query returned zero rows", durationMs: 40 },
            error: "query returned zero rows",
          },
        })}
        question="q"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("SQL error")).toBeTruthy();
    expect(screen.getByText(/zero rows/)).toBeTruthy();
  });
});

describe("AgentMessageBubble — outcome-distinct rendering", () => {
  it("clarify renders as information, not failure", () => {
    render(
      <AgentMessageBubble
        turn={doneTurn({
          envelope: {
            outcome: "clarify",
            summary: "Which season did you mean?",
            sql: null,
            result: null,
            error: null,
          },
        })}
        question="q"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("Needs clarification")).toBeTruthy();
    expect(screen.getByText("Which season did you mean?")).toBeTruthy();
    expect(screen.queryByText("SQL used")).toBeNull();
  });

  it("decline is styled distinctly from clarify and from the stream-error state", () => {
    render(
      <AgentMessageBubble
        turn={doneTurn({
          envelope: {
            outcome: "decline",
            summary: "The dataset has no salary information.",
            sql: null,
            result: null,
            error: null,
          },
        })}
        question="q"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("Can't answer this")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    expect(screen.queryByText("Try again")).toBeNull();
  });
});

describe("AgentMessageBubble — streaming and error states", () => {
  it("shows per-node progress while streaming", () => {
    render(
      <AgentMessageBubble
        turn={{
          role: "assistant",
          id: "t2",
          createdAt: Date.now(),
          status: "streaming",
          nodeProgress: [{ node: "classify", durationMs: 12 }],
          envelope: null,
          error: null,
        }}
        question="q"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("Classifying question")).toBeTruthy();
  });

  it("a stream-level error offers retry with the original question", () => {
    const onRetry = vi.fn();
    render(
      <AgentMessageBubble
        turn={{
          role: "assistant",
          id: "t3",
          createdAt: Date.now(),
          status: "error",
          nodeProgress: [],
          envelope: null,
          error: "agent service unreachable: fetch failed",
        }}
        question="Who led the league in points?"
        onOpenInEditor={vi.fn()}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText("Something went wrong")).toBeTruthy();
    fireEvent.click(screen.getByText("Try again"));
    expect(onRetry).toHaveBeenCalledWith("Who led the league in points?");
  });
});

/**
 * A stored turn whose envelope carries an error but no result. Produced by an
 * interrupted stream (conversations.ts's INTERRUPTED) and by summarize's
 * retries-exhausted path, which returns `{outcome: "answer", error}` and no summary.
 *
 * Before this was rendered, such a turn showed an outcome badge and nothing else —
 * the whole visible symptom of the 2026-08-10 report: a slow question answered with
 * a blank bubble while the explanation sat unread in the database.
 */
describe("an envelope carrying an error", () => {
  it("shows the error when there is no result to carry it", () => {
    render(
      <AgentMessageBubble
        turn={doneTurn({
          envelope: {
            outcome: "answer",
            summary: "",
            sql: null,
            result: null,
            error: "this answer was interrupted before the agent finished",
          },
        })}
        question="Who scored the most points this season"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("this answer was interrupted before the agent finished")).toBeTruthy();
  });

  it("does not report the same failure twice when a result already carries it", () => {
    render(
      <AgentMessageBubble
        turn={doneTurn({
          envelope: {
            outcome: "answer",
            summary: "",
            sql: "SELECT bad",
            result: { status: "error", error: "column does not exist", durationMs: 12 },
            error: "column does not exist",
          },
        })}
        question="q"
        onOpenInEditor={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getAllByText(/column does not exist/)).toHaveLength(1);
  });
});
