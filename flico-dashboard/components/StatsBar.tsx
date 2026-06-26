"use client";

import type { Call } from "@/lib/types";

function startOfToday(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function startOfWeek(): number {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return d.getTime();
}

export default function StatsBar({ calls }: { calls: Call[] }) {
  const todayStart = startOfToday();
  const weekStart = startOfWeek();

  const today = calls.filter((c) => new Date(c.started_at).getTime() >= todayStart);
  const week = calls.filter((c) => new Date(c.started_at).getTime() >= weekStart);

  const completed = calls.filter((c) => c.status === "completed");
  const avgDuration =
    completed.length > 0
      ? Math.round(
          completed.reduce((acc, c) => acc + (c.duration_seconds || 0), 0) /
            completed.length,
        )
      : 0;

  const captured = calls.filter(
    (c) => c.captured_name || c.captured_phone || c.captured_email,
  );
  const conversionRate =
    calls.length > 0 ? Math.round((captured.length / calls.length) * 100) : 0;

  const inProgress = calls.filter((c) => c.status === "in_progress").length;

  const stats: { label: string; value: string; sub?: string; tone?: string }[] = [
    {
      label: "Today",
      value: today.length.toString(),
      sub: `${week.length} this week`,
    },
    {
      label: "Avg duration",
      value: avgDuration > 0 ? formatDuration(avgDuration) : "—",
      sub: `${completed.length} completed`,
    },
    {
      label: "Captured details",
      value: `${conversionRate}%`,
      sub: `${captured.length} of ${calls.length} calls`,
    },
    {
      label: "Live now",
      value: inProgress.toString(),
      sub: inProgress > 0 ? "calls in progress" : "no active calls",
      tone: inProgress > 0 ? "live" : undefined,
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-6">
      {stats.map((s) => (
        <div
          key={s.label}
          className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4"
        >
          <div className="flex items-center justify-between">
            <div className="text-xs uppercase tracking-wide text-neutral-500">
              {s.label}
            </div>
            {s.tone === "live" && (
              <span className="flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500"></span>
                </span>
                live
              </span>
            )}
          </div>
          <div className="text-2xl font-semibold mt-2">{s.value}</div>
          {s.sub && <div className="text-xs text-neutral-500 mt-1">{s.sub}</div>}
        </div>
      ))}
    </div>
  );
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${s}s`;
}
