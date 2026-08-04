"use client";

import { EXAMPLES } from "../lib/examples";

type Props = {
  onSelect: (sql: string) => void;
};

export default function ExampleList({ onSelect }: Props) {
  return (
    <div className="flex flex-col gap-1">
      <h2 className="px-1 text-xs font-semibold uppercase tracking-wide text-ink-faint">
        Examples
      </h2>
      <ul>
        {EXAMPLES.map((example) => (
          <li key={example.title}>
            <button
              type="button"
              onClick={() => onSelect(example.sql)}
              className="w-full rounded-panel px-2 py-1 text-left text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
            >
              {example.title}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
