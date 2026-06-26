import { createClient } from "@/lib/supabase-server";
import CallsTable from "@/components/CallsTable";
import Header from "@/components/Header";
import type { Call } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function Home() {
  const supabase = await createClient();
  const { data: calls } = await supabase
    .from("calls")
    .select("*")
    .order("started_at", { ascending: false })
    .limit(500);

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <Header />
      <CallsTable initial={(calls || []) as Call[]} />
    </main>
  );
}
