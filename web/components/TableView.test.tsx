// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import TableView from "./TableView";
import type { OkResult } from "../lib/api-types";

afterEach(cleanup);

function ok(columns: string[], rows: unknown[][]): OkResult {
  return {
    status: "ok",
    columns,
    rows,
    rowCount: rows.length,
    truncated: false,
    durationMs: 1,
  };
}

describe("TableView", () => {
  it("renders duplicate column names with each column's own data", () => {
    render(
      <TableView
        result={ok(
          ["pts", "pts"],
          [
            [10, 20],
            [30, 40],
          ],
        )}
      />,
    );
    expect(screen.getAllByText("pts")).toHaveLength(2);
    for (const value of ["10", "20", "30", "40"]) {
      expect(screen.getByText(value)).toBeTruthy();
    }
  });

  it("renders NULL distinguishably from empty string", () => {
    render(<TableView result={ok(["a", "b"], [[null, ""]])} />);
    const nullCell = screen.getByText("NULL");
    expect(nullCell.className).toContain("italic");
    const cells = document.querySelectorAll("td");
    expect(cells).toHaveLength(2);
    expect(cells[1].textContent).toBe("");
  });

  it("sorts client-side on header click", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<TableView result={ok(["n"], [[3], [1], [2]])} />);
    await userEvent.click(screen.getByText("n"));
    const values = [...document.querySelectorAll("td")].map((td) => td.textContent);
    expect(values).toEqual(["1", "2", "3"]);
  });
});
