"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { createClient } from "@/lib/supabase-browser";
import { formatDateTime, formatDuration } from "@/lib/format";
import type { Call } from "@/lib/types";
import StatsBar from "@/components/StatsBar";

export default function CallsTable({ initial }: { initial: Call[] }) {
  const [calls, setCalls] = useState<Call[]>(initial);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "captured" | "in_progress">("all");
  const [connected, setConnected] = useState(false);
  const [expandedSummaries, setExpandedSummaries] = useState<Set<string>>(
    () => new Set(),
  );

  const toggleSummary = (callSid: string) => {
    setExpandedSummaries((prev) => {
      const next = new Set(prev);
      if (next.has(callSid)) next.delete(callSid);
      else next.add(callSid);
      return next;
    });
  };

  useEffect(() => {
    const supabase = createClient();

    const channel = supabase
      .channel("calls-changes")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "calls" },
        (payload) => {
          setCalls((current) => {
            if (payload.eventType === "INSERT") {
              const row = payload.new as Call;
              if (current.some((c) => c.call_sid === row.call_sid))
                return current;
              return [row, ...current];
            }
            if (payload.eventType === "UPDATE") {
              const row = payload.new as Call;
              return current.map((c) =>
                c.call_sid === row.call_sid ? { ...c, ...row } : c,
              );
            }
            if (payload.eventType === "DELETE") {
              const row = payload.old as Partial<Call>;
              return current.filter((c) => c.call_sid !== row.call_sid);
            }
            return current;
          });
        },
      )
      .subscribe((status) => {
        setConnected(status === "SUBSCRIBED");
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return calls.filter((c) => {
      if (filter === "captured" && !c.captured_name && !c.captured_phone && !c.captured_email)
        return false;
      if (filter === "in_progress" && c.status !== "in_progress") return false;
      if (!q) return true;
      const haystack = [
        c.from_number,
        c.captured_name,
        c.captured_phone,
        c.captured_email,
        c.call_sid,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [calls, query, filter]);

  const groups = useMemo(() => groupByDay(filtered), [filtered]);

  return (
    <div>
      <StatsBar calls={calls} />

      <div className="mt-8 flex flex-col sm:flex-row sm:items-center gap-3">
        <div className="relative flex-1">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-400"
          >
            <path
              fillRule="evenodd"
              d="M9 3.5a5.5 5.5 0 1 0 3.471 9.788l3.07 3.07a.75.75 0 1 0 1.06-1.06l-3.07-3.07A5.5 5.5 0 0 0 9 3.5ZM5 9a4 4 0 1 1 8 0 4 4 0 0 1-8 0Z"
              clipRule="evenodd"
            />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by name, phone, email, or caller number…"
            className="w-full rounded-lg border border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 pl-9 pr-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-neutral-200 dark:border-neutral-800 p-1 text-sm">
          {(
            [
              ["all", "All"],
              ["captured", "Captured"],
              ["in_progress", "Live"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={`px-3 py-1 rounded-md ${
                filter === key
                  ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
                  : "text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <ConnectionBadge connected={connected} />
      </div>

      {filtered.length === 0 ? (
        <EmptyState hasAny={calls.length > 0} />
      ) : (
        <div className="mt-4 space-y-6">
          {groups.map((group) => (
            <div key={group.label}>
              <div className="text-xs uppercase tracking-wide text-neutral-500 mb-2 px-1">
                {group.label}
                <span className="ml-2 text-neutral-400">
                  · {group.calls.length}
                </span>
              </div>
              <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900">
                <table className="w-full text-sm">
                  <thead className="bg-neutral-50 dark:bg-neutral-900/60 text-left">
                    <tr>
                      <th className="px-4 py-3 font-medium">When</th>
                      <th className="px-4 py-3 font-medium">From</th>
                      <th className="px-4 py-3 font-medium">Caller</th>
                      <th className="px-4 py-3 font-medium">Summary</th>
                      <th className="px-4 py-3 font-medium">Phone</th>
                      <th className="px-4 py-3 font-medium">Email</th>
                      <th className="px-4 py-3 font-medium">Duration</th>
                      <th className="px-4 py-3 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.calls.map((c) => (
                      <tr
                        key={c.call_sid}
                        className="border-t border-neutral-200 dark:border-neutral-800 hover:bg-neutral-50 dark:hover:bg-neutral-900/40"
                      >
                        <td className="px-4 py-3">
                          <Link
                            href={`/calls/${c.call_sid}`}
                            className="hover:underline whitespace-nowrap"
                          >
                            {formatDateTime(c.started_at)}
                          </Link>
                        </td>
                        <td className="px-4 py-3 font-mono text-xs">
                          {c.from_number || "—"}
                        </td>
                        <td className="px-4 py-3">
                          {c.captured_name || (
                            <span className="text-neutral-400">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-neutral-600 dark:text-neutral-400 italic max-w-xs align-top">
                          {c.summary ? (
                            <button
                              type="button"
                              onClick={() => toggleSummary(c.call_sid)}
                              className={`text-left w-full cursor-pointer hover:text-neutral-900 dark:hover:text-white transition-colors ${
                                expandedSummaries.has(c.call_sid)
                                  ? "whitespace-normal"
                                  : "truncate block"
                              }`}
                              title={
                                expandedSummaries.has(c.call_sid)
                                  ? "Click to collapse"
                                  : "Click to expand"
                              }
                            >
                              {c.summary}
                            </button>
                          ) : (
                            <span className="text-neutral-400 not-italic">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs">
                          {c.captured_phone || (
                            <span className="text-neutral-400">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs">
                          {c.captured_email || (
                            <span className="text-neutral-400">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {formatDuration(c.duration_seconds)}
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge status={c.status} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string | null }) {
  const s = status || "unknown";
  const color =
    s === "completed"
      ? "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300"
      : s === "in_progress"
        ? "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300"
        : "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded-full text-xs ${color}`}
    >
      {s.replace("_", " ")}
    </span>
  );
}

function ConnectionBadge({ connected }: { connected: boolean }) {
  return (
    <div
      className="hidden sm:flex items-center gap-2 text-xs text-neutral-500"
      title={connected ? "Realtime stream connected" : "Reconnecting…"}
    >
      <span className="relative flex h-2 w-2">
        {connected && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75"></span>
        )}
        <span
          className={`relative inline-flex rounded-full h-2 w-2 ${
            connected ? "bg-green-500" : "bg-neutral-400"
          }`}
        ></span>
      </span>
      {connected ? "Live" : "Connecting"}
    </div>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div className="mt-10 rounded-xl border border-dashed border-neutral-300 dark:border-neutral-800 p-10 text-center">
      <div className="text-4xl mb-3">📞</div>
      {hasAny ? (
        <p className="text-sm text-neutral-500">
          No calls match this filter.
        </p>
      ) : (
        <>
          <h3 className="text-lg font-medium">No calls yet</h3>
          <p className="text-sm text-neutral-500 mt-1">
            When someone calls Flico, the call will show up here in real time.
          </p>
        </>
      )}
    </div>
  );
}

function groupByDay(
  calls: Call[],
): { label: string; calls: Call[] }[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);

  const groups: Record<string, Call[]> = {
    Today: [],
    Yesterday: [],
    "This week": [],
    Earlier: [],
  };

  for (const c of calls) {
    const t = new Date(c.started_at).getTime();
    if (t >= today.getTime()) groups.Today.push(c);
    else if (t >= yesterday.getTime()) groups.Yesterday.push(c);
    else if (t >= weekAgo.getTime()) groups["This week"].push(c);
    else groups.Earlier.push(c);
  }

  return Object.entries(groups)
    .filter(([, v]) => v.length > 0)
    .map(([label, calls]) => ({ label, calls }));
}
