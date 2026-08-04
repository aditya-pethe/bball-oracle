"use client";

import { useMemo } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { sql, PostgreSQL } from "@codemirror/lang-sql";
import type { SchemaTable } from "../lib/api-types";

const MAX_QUERY_CHARS = 20_000;

type Props = {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  disabled: boolean;
  schema: SchemaTable[];
};

export default function SqlEditor({ value, onChange, onRun, disabled, schema }: Props) {
  const extensions = useMemo(
    () => [
      sql({
        dialect: PostgreSQL,
        schema: Object.fromEntries(
          schema.map((table) => [
            `nba.${table.name}`,
            table.columns.map((column) => column.name),
          ]),
        ),
      }),
    ],
    [schema],
  );

  return (
    <div className="flex min-h-0 flex-col">
      <div
        className="min-h-0 flex-1 overflow-auto rounded-panel border border-edge"
        // Capture phase so CodeMirror's own Mod-Enter binding (insert blank line)
        // never sees the event; also keeps the run callback out of the extension
        // array, which would otherwise rebuild the editor to stay current.
        onKeyDownCapture={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            event.stopPropagation();
            if (!disabled) onRun();
          }
        }}
      >
        <CodeMirror
          value={value}
          onChange={onChange}
          extensions={extensions}
          theme="dark"
          placeholder="SELECT ... FROM nba.pbp_event"
          style={{ fontFamily: "var(--font-mono)", fontSize: "13px" }}
        />
      </div>
      {value.length > MAX_QUERY_CHARS && (
        <p className="mt-1 text-xs text-warning">
          {value.length.toLocaleString()} characters — the server rejects queries over{" "}
          {MAX_QUERY_CHARS.toLocaleString()}.
        </p>
      )}
    </div>
  );
}
