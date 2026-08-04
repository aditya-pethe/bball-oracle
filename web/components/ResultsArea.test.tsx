// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import ResultsArea from "./ResultsArea";
import type { OkResult } from "../lib/api-types";

afterEach(cleanup);

const okResult: OkResult = {
  status: "ok",
  columns: ["player", "pts"],
  rows: [["Luka Doncic", 73]],
  rowCount: 1,
  truncated: false,
  durationMs: 42,
};

describe("ResultsArea state rendering", () => {
  it("idle shows the empty-state hint", () => {
    render(<ResultsArea state="idle" />);
    expect(screen.getByText(/run a query/i)).toBeTruthy();
  });

  it("running shows the in-flight indicator", () => {
    render(<ResultsArea state="running" />);
    expect(screen.getByText(/running/i)).toBeTruthy();
  });

  it("ok renders row count, duration, and the table", () => {
    render(<ResultsArea state={okResult} />);
    expect(screen.getByText("1 row")).toBeTruthy();
    expect(screen.getByText("42 ms")).toBeTruthy();
    expect(screen.getByText("Luka Doncic")).toBeTruthy();
    expect(screen.queryByText(/truncated/i)).toBeNull();
  });

  it("ok + truncated shows a distinct truncation notice", () => {
    render(
      <ResultsArea
        state={{ ...okResult, truncated: true, rowCount: 1000 }}
      />,
    );
    expect(screen.getByText(/truncated — showing the first 1,000 rows/i)).toBeTruthy();
  });

  it("validation rejection shows the reason, labeled as rejection not SQL error", () => {
    render(
      <ResultsArea
        state={{ status: "validation_rejected", error: "only SELECT statements are allowed" }}
      />,
    );
    expect(screen.getByText("Query rejected")).toBeTruthy();
    expect(screen.getByText(/only SELECT statements/)).toBeTruthy();
    expect(screen.queryByText("SQL error")).toBeNull();
  });

  it("SQL error shows the server message", () => {
    render(
      <ResultsArea
        state={{ status: "error", error: 'relation "nba.nope" does not exist', durationMs: 5 }}
      />,
    );
    expect(screen.getByText("SQL error")).toBeTruthy();
    expect(screen.getByText(/does not exist/)).toBeTruthy();
  });

  it("timeout is distinct from a SQL error", () => {
    render(
      <ResultsArea
        state={{ status: "timeout", error: "query exceeded the 5s time limit", durationMs: 5001 }}
      />,
    );
    expect(screen.getByText("Query timed out")).toBeTruthy();
    expect(screen.queryByText("SQL error")).toBeNull();
  });

  it("rate limit shows retry-after", () => {
    render(
      <ResultsArea
        state={{ status: "rate_limited", error: "rate limit exceeded", retryAfterSeconds: 37 }}
      />,
    );
    expect(screen.getByText("Rate limited")).toBeTruthy();
    expect(screen.getByText(/Retry in 37s/)).toBeTruthy();
  });
});
