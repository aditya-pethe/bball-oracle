"use client";

import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import type { OkResult } from "../lib/api-types";

function Cell({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="italic text-ink-faint">NULL</span>;
  }
  if (typeof value === "object") return <>{JSON.stringify(value)}</>;
  return <>{String(value)}</>;
}

const helper = createColumnHelper<unknown[]>();

export default function TableView({ result }: { result: OkResult }) {
  const [sorting, setSorting] = useState<SortingState>([]);

  // Index-based ids and accessors: duplicate column names must each keep their own data.
  const columns = useMemo<ColumnDef<unknown[], unknown>[]>(
    () =>
      result.columns.map((name, i) =>
        helper.accessor((row) => row[i], {
          id: String(i),
          header: name,
          cell: (info) => <Cell value={info.getValue()} />,
          sortingFn: "basic",
          sortUndefined: "last",
        }),
      ),
    [result.columns],
  );

  const table = useReactTable({
    data: result.rows,
    columns,
    state: { sorting },
    sortDescFirst: false,
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-panel border border-edge">
      <table className="w-full border-collapse font-mono text-xs">
        <thead className="sticky top-0 bg-surface-raised">
          <tr>
            {table.getFlatHeaders().map((header) => (
              <th
                key={header.id}
                onClick={header.column.getToggleSortingHandler()}
                className="cursor-pointer select-none whitespace-nowrap border-b border-edge px-3 py-2 text-left font-semibold text-ink"
              >
                {flexRender(header.column.columnDef.header, header.getContext())}
                {{ asc: " ↑", desc: " ↓" }[header.column.getIsSorted() as string] ?? ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id} className="border-b border-edge/50 hover:bg-surface-raised/60">
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="whitespace-nowrap px-3 py-1.5 text-ink-muted">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
