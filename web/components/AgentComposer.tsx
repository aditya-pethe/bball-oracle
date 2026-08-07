"use client";

type Props = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (question: string) => void;
  disabled: boolean;
};

export default function AgentComposer({ value, onChange, onSubmit, disabled }: Props) {
  const submit = () => {
    const trimmed = value.trim();
    if (trimmed === "" || disabled) return;
    onSubmit(trimmed);
  };

  return (
    <div className="flex items-end gap-2">
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            submit();
          }
        }}
        disabled={disabled}
        placeholder="Ask a question about the 2023-24 season…"
        rows={2}
        className="min-h-0 flex-1 resize-none rounded-panel border border-edge bg-surface-inset px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:outline-none disabled:opacity-60"
      />
      <button
        type="button"
        onClick={submit}
        disabled={disabled || value.trim() === ""}
        className="rounded-panel bg-accent px-4 py-2 text-sm font-semibold text-accent-ink hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {disabled ? "Thinking…" : "Ask"}
      </button>
    </div>
  );
}
