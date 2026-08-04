"use client";

import { useState } from "react";
import type { SchemaTable } from "../lib/api-types";

const TYPE_ABBREVIATIONS: Record<string, string> = {
  smallint: "int",
  integer: "int",
  bigint: "bigint",
  "double precision": "float",
  "character varying": "text",
  "timestamp without time zone": "ts",
  "timestamp with time zone": "tstz",
};

type Props = {
  schema: SchemaTable[];
  onInsert: (name: string) => void;
};

export default function SchemaBrowser({ schema, onInsert }: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  if (schema.length === 0) {
    return <p className="px-1 text-xs text-ink-faint">Loading schema…</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      <h2 className="px-1 text-xs font-semibold uppercase tracking-wide text-ink-faint">
        Tables
      </h2>
      {schema.map((table) => (
        <div key={table.name} className="rounded-panel border border-edge">
          <div className="flex items-center">
            <button
              type="button"
              aria-label={`${collapsed[table.name] ? "Expand" : "Collapse"} ${table.name}`}
              onClick={() =>
                setCollapsed((prev) => ({ ...prev, [table.name]: !prev[table.name] }))
              }
              className="px-2 py-1.5 text-xs text-ink-faint hover:text-ink"
            >
              {collapsed[table.name] ? "▸" : "▾"}
            </button>
            <button
              type="button"
              onClick={() => onInsert(`nba.${table.name}`)}
              title={`Insert nba.${table.name}`}
              className="flex-1 py-1.5 pr-2 text-left font-mono text-xs font-semibold text-ink hover:text-accent"
            >
              {table.name}
            </button>
          </div>
          {!collapsed[table.name] && (
            <ul className="border-t border-edge/60 py-1">
              {table.columns.map((column) => (
                <li key={column.name}>
                  <button
                    type="button"
                    onClick={() => onInsert(column.name)}
                    title={`Insert ${column.name}`}
                    className="flex w-full items-baseline justify-between gap-2 px-3 py-0.5 text-left font-mono text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
                  >
                    <span className="truncate">{column.name}</span>
                    <span className="shrink-0 text-ink-faint">
                      {TYPE_ABBREVIATIONS[column.type] ?? column.type}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}
