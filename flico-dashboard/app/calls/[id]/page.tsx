import { notFound } from "next/navigation";
import Link from "next/link";
import { createClient } from "@/lib/supabase-server";
import { formatDateTime, formatDuration } from "@/lib/format";
import type { Call } from "@/lib/types";
import CopyableField from "@/components/CopyableField";

export const dynamic = "force-dynamic";

export default async function CallDetail({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("calls")
    .select("*")
    .eq("call_sid", id)
    .single();

  if (error || !data) notFound();
  const call = data as Call;

  const turns = call.transcript || [];
  const userTurns = turns.filter((t) => t.role === "user").length;
  const agentTurns = turns.filter((t) => t.role === "assistant").length;

  const whatsappHref = call.captured_phone
    ? `https://wa.me/${call.captured_phone.replace(/[^0-9]/g, "")}`
    : undefined;
  const emailHref = call.captured_email
    ? `mailto:${call.captured_email}`
    : undefined;
  const phoneTelHref = call.from_number ? `tel:${call.from_number}` : undefined;

  return (
    <main className="mx-auto max-w-4xl px-6 py-8">
      <Link
        href="/"
        className="text-sm text-neutral-500 hover:text-neutral-900 dark:hover:text-white inline-flex items-center gap-1"
      >
        ← Back to calls
      </Link>

      <section className="mt-4 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-gradient-to-br from-white to-neutral-50 dark:from-neutral-900 dark:to-neutral-950 p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold">
                {call.captured_name || "Anonymous caller"}
              </h1>
              <StatusBadge status={call.status} />
            </div>
            <p className="text-sm text-neutral-500 mt-1">
              Called {formatDateTime(call.started_at)} ·{" "}
              {formatDuration(call.duration_seconds)} ·{" "}
              {userTurns + agentTurns} turns
            </p>
            <p className="font-mono text-xs text-neutral-400 mt-2">
              {call.call_sid}
            </p>
            {call.summary && (
              <div className="mt-4">
                <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-1">
                  Summary
                </div>
                <div className="rounded-lg border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-950/20 p-3 text-sm">
                  {call.summary}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-6">
          <CopyableField
            label="From"
            value={call.from_number}
            mono
            href={phoneTelHref}
          />
          <CopyableField
            label="Language"
            value={call.language || null}
          />
          <CopyableField
            label="Captured phone"
            value={call.captured_phone}
            mono
            href={whatsappHref}
          />
          <CopyableField
            label="Captured email"
            value={call.captured_email}
            mono
            href={emailHref}
          />
        </div>

        {(whatsappHref || emailHref) && (
          <div className="mt-4 flex flex-wrap gap-2">
            {whatsappHref && (
              <a
                href={whatsappHref}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-green-600 text-white text-sm hover:bg-green-700"
              >
                Open in WhatsApp
              </a>
            )}
            {emailHref && (
              <a
                href={emailHref}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700"
              >
                Send email
              </a>
            )}
          </div>
        )}
      </section>

      <section className="mt-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-medium">Transcript</h2>
          <div className="text-xs text-neutral-500">
            {userTurns} caller · {agentTurns} Fiona
          </div>
        </div>

        {turns.length === 0 ? (
          <div className="rounded-xl border border-dashed border-neutral-300 dark:border-neutral-800 p-10 text-center text-sm text-neutral-500">
            No transcript captured for this call.
          </div>
        ) : (
          <div className="space-y-3">
            {turns.map((turn, i) => (
              <div
                key={i}
                className={`flex ${turn.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm ${
                    turn.role === "user"
                      ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 rounded-br-md"
                      : "bg-blue-50 dark:bg-blue-950/30 text-neutral-900 dark:text-neutral-100 border border-blue-100 dark:border-blue-900/40 rounded-bl-md"
                  }`}
                >
                  <div className="text-[10px] uppercase tracking-wide opacity-60 mb-0.5">
                    {turn.role === "user" ? "Caller" : "Fiona"}
                  </div>
                  <div className="whitespace-pre-wrap">{turn.text}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </main>
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
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs ${color}`}>
      {s.replace("_", " ")}
    </span>
  );
}
