"use client";

import { useState } from "react";

export default function CopyableField({
  label,
  value,
  mono = false,
  href,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
  href?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // ignore
    }
  }

  const display = value || "—";
  const interactive = !!value;

  return (
    <div className="rounded-lg border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-3">
      <div className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className="mt-1 flex items-center justify-between gap-2">
        {href && value ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className={`hover:underline ${mono ? "font-mono text-sm" : ""}`}
          >
            {display}
          </a>
        ) : (
          <span className={mono ? "font-mono text-sm" : ""}>{display}</span>
        )}
        {interactive && (
          <button
            onClick={copy}
            className="text-xs text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
          >
            {copied ? "copied" : "copy"}
          </button>
        )}
      </div>
    </div>
  );
}
