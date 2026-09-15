"use client";

import { FormEvent, useMemo, useState } from "react";
import { postFactoryAction, type FactoryAction } from "./factory-api";
import { Icon, Panel, Pill, SectionHeading, Toggle } from "./FactoryPrimitives";

type Capability = { id: string; label: string; description: string };
type TimelineEvent = {
  id: string;
  title: string;
  detail: string;
  time: string;
  tone: "done" | "current" | "pending";
};

const capabilities: Capability[] = [
  { id: "bookings", label: "Bookings", description: "Collect dates and guest details for owner review." },
  { id: "handoff", label: "Human handoff", description: "Offer a warm transfer when confidence or policy requires it." },
  { id: "after-hours", label: "After-hours capture", description: "Capture a callback request outside the support window." },
  { id: "notifications", label: "Owner notifications", description: "Prepare reviewable notifications for the approved channel." },
];

const initialTimeline: TimelineEvent[] = [
  { id: "created", title: "Factory workspace created", detail: "Owner-only review lane initialized", time: "Today · 09:14", tone: "done" },
  { id: "intake", title: "Company intake is next", detail: "No customer data or credentials have been requested", time: "Waiting", tone: "current" },
  { id: "sources", title: "Knowledge staging", detail: "Awaiting an approved source reference", time: "Queued", tone: "pending" },
  { id: "plan", title: "Plan digest confirmation", detail: "A signed review step is required before handoff", time: "Locked", tone: "pending" },
];

const tabs = [
  ["intake", "1", "Intake"],
  ["staging", "2", "Staging"],
  ["review", "3", "Review"],
  ["handoff", "4", "Handoff"],
] as const;

function currentTime() {
  return new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(new Date());
}

export default function FactoryConsole() {
  const [activeTab, setActiveTab] = useState<(typeof tabs)[number][0]>("intake");
  const [company, setCompany] = useState({ name: "", slug: "", contact: "", email: "", language: "English" });
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [source, setSource] = useState({ name: "", kind: "Approved document", notes: "" });
  const [digestConfirmed, setDigestConfirmed] = useState(false);
  const [timeline, setTimeline] = useState(initialTimeline);
  const [pending, setPending] = useState<{ action: FactoryAction; title: string; detail: string; payload: Record<string, unknown> } | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "warning"; text: string } | null>(null);

  const enabledCount = useMemo(() => Object.values(enabled).filter(Boolean).length, [enabled]);
  const canConfirmPlan = digestConfirmed && Boolean(company.name.trim()) && Boolean(source.name.trim());

  function beginAction(action: FactoryAction, title: string, detail: string, payload: Record<string, unknown>) {
    setNotice(null);
    setPending({ action, title, detail, payload });
  }

  async function confirmAction() {
    if (!pending) return;
    const action = pending;
    setPending(null);
    try {
      await postFactoryAction(action.action, action.payload);
      setNotice({ tone: "success", text: `${action.title} request accepted by the review lane.` });
    } catch {
      setNotice({ tone: "warning", text: `${action.title} is staged locally. The same-origin API placeholder is not connected yet.` });
    }
    setTimeline((current) => [
      ...current,
      { id: `${action.action}-${Date.now()}`, title: action.title, detail: "Owner confirmation recorded · append-only", time: currentTime(), tone: "done" },
    ]);
  }

  function submitIntake(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    beginAction("intake", "Save company intake", "This records the company profile for owner review.", { company });
  }

  function stageSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    beginAction("sources", "Stage knowledge source", "Only source metadata is sent; the console never accepts provider credentials or fetches arbitrary URLs.", { source });
  }

  function saveCapabilities() {
    beginAction("capabilities", "Save capabilities", "Only explicitly enabled capabilities will be included in the review plan.", { capabilities: enabled });
  }

  function confirmPlan() {
    beginAction("plan/confirm", "Confirm plan digest", "This acknowledges the immutable plan digest for review handoff.", { digest: "sha256:7b3f…9c21", confirmed: true });
  }

  function openHandoff() {
    beginAction("handoff", "Prepare PR handoff", "This creates a review-only handoff request. Deployment remains unavailable.", { generation: "factory-review", deployment: false });
  }

  return (
    <main className="min-h-screen bg-[#f6f8fb] text-slate-950">
      <div className="mx-auto max-w-[1480px] px-4 py-5 sm:px-6 lg:px-10 lg:py-8">
        <header className="flex flex-col gap-6 border-b border-slate-200 pb-7 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-start gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-slate-950 text-sm font-bold tracking-tight text-white shadow-sm">F/</div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-semibold tracking-tight text-slate-950">SmartPBX Factory</p>
                <Pill tone="amber">Owner-only</Pill>
              </div>
              <h1 className="mt-2 text-3xl font-semibold tracking-[-0.04em] text-slate-950 sm:text-4xl">Review before anything runs.</h1>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">A private workspace for assembling a SmartPBX agent plan, checking its provenance, and handing off a reviewable PR.</p>
            </div>
          </div>
          <div className="flex items-center gap-2 self-start rounded-full border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-500 shadow-sm">
            <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden="true" />
            Review lane · no deployment access
          </div>
        </header>

        <div className="mt-7 grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div>
            <nav aria-label="Factory setup steps" className="mb-7 overflow-x-auto">
              <ol className="flex min-w-max items-center gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm">
                {tabs.map(([id, number, label], index) => (
                  <li key={id} className="flex items-center gap-2">
                    <button type="button" onClick={() => setActiveTab(id)} className={`flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-cyan-500 focus:ring-offset-1 ${activeTab === id ? "bg-slate-950 text-white" : "text-slate-500 hover:bg-slate-50 hover:text-slate-900"}`} aria-current={activeTab === id ? "step" : undefined}>
                      <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] ${activeTab === id ? "bg-cyan-400 text-slate-950" : "bg-slate-100 text-slate-500"}`}>{number}</span>
                      {label}
                    </button>
                    {index < tabs.length - 1 ? <span className="text-slate-300" aria-hidden="true">/</span> : null}
                  </li>
                ))}
              </ol>
            </nav>

            {notice ? <div className={`mb-5 rounded-xl border px-4 py-3 text-sm ${notice.tone === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-amber-200 bg-amber-50 text-amber-800"}`} role="status">{notice.text}</div> : null}

            {activeTab === "intake" ? (
              <div className="space-y-6">
                <Panel className="p-5 sm:p-7">
                  <SectionHeading eyebrow="Company intake" title="Start with the operating context." description="Capture only the details needed to draft a plan. Secrets, infrastructure credentials, and arbitrary URLs do not belong in this console." />
                  <form onSubmit={submitIntake} className="grid gap-5 sm:grid-cols-2">
                    <label className="sm:col-span-2"><span className="mb-1.5 block text-xs font-semibold text-slate-700">Company name</span><input required value={company.name} onChange={(e) => setCompany({ ...company, name: e.target.value })} placeholder="e.g. Northstar Hotels" className="factory-field" /></label>
                    <label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Workspace slug</span><input required value={company.slug} onChange={(e) => setCompany({ ...company, slug: e.target.value })} placeholder="northstar-hotels" className="factory-field" /></label>
                    <label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Primary language</span><select value={company.language} onChange={(e) => setCompany({ ...company, language: e.target.value })} className="factory-field"><option>English</option><option>Korean</option><option>Spanish</option><option>French</option></select></label>
                    <label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Owner contact</span><input required value={company.contact} onChange={(e) => setCompany({ ...company, contact: e.target.value })} placeholder="Name" className="factory-field" /></label>
                    <label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Review email</span><input required type="email" value={company.email} onChange={(e) => setCompany({ ...company, email: e.target.value })} placeholder="owner@example.com" className="factory-field" /></label>
                    <div className="flex items-center justify-between gap-4 border-t border-slate-100 pt-5 sm:col-span-2"><p className="text-xs leading-5 text-slate-500">Saving opens a confirmation screen and creates an append-only timeline entry.</p><button type="submit" className="factory-button-primary">Review intake <Icon name="arrow" /></button></div>
                  </form>
                </Panel>
                <Panel className="p-5 sm:p-7"><div className="flex items-start gap-3"><div className="rounded-lg bg-cyan-50 p-2 text-cyan-700"><Icon name="lock" /></div><div><h2 className="text-sm font-semibold text-slate-900">Private by construction</h2><p className="mt-1 text-sm leading-6 text-slate-500">This lane is designed for owner review. It never asks for provider, VPS, DNS, or Cloudflare tokens, and it cannot deploy an agent.</p></div></div></Panel>
              </div>
            ) : null}

            {activeTab === "staging" ? (
              <div className="space-y-6">
                <Panel className="p-5 sm:p-7">
                  <SectionHeading eyebrow="Knowledge staging" title="Name the source. Keep the content elsewhere." description="Stage an approved source reference for review without pasting secrets or granting the console a fetchable URL." />
                  <form onSubmit={stageSource} className="space-y-5">
                    <div className="grid gap-5 sm:grid-cols-2"><label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Source label</span><input required value={source.name} onChange={(e) => setSource({ ...source, name: e.target.value })} placeholder="Northstar guest FAQ" className="factory-field" /></label><label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Source kind</span><select value={source.kind} onChange={(e) => setSource({ ...source, kind: e.target.value })} className="factory-field"><option>Approved document</option><option>Owner-provided transcript</option><option>Existing knowledge collection</option></select></label></div>
                    <label><span className="mb-1.5 block text-xs font-semibold text-slate-700">Review note <span className="font-normal text-slate-400">(optional)</span></span><textarea value={source.notes} onChange={(e) => setSource({ ...source, notes: e.target.value })} placeholder="What should the reviewer verify?" className="factory-field min-h-24 resize-y" /></label>
                    <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-4"><div className="flex items-center gap-3"><div className="rounded-lg bg-white p-2 text-slate-500 shadow-sm"><Icon name="plus" /></div><div><p className="text-sm font-medium text-slate-800">No direct upload or URL fetch</p><p className="mt-1 text-xs leading-5 text-slate-500">An approved backend adapter can attach the source after owner confirmation. This browser only carries the label and review note.</p></div></div></div>
                    <div className="flex items-center justify-between gap-4 border-t border-slate-100 pt-5"><p className="text-xs leading-5 text-slate-500">Source metadata is sent only after confirmation.</p><button type="submit" className="factory-button-primary">Stage source <Icon name="arrow" /></button></div>
                  </form>
                </Panel>
                <Panel className="p-5 sm:p-7"><div className="flex items-center justify-between"><div><h2 className="text-sm font-semibold text-slate-900">Staged sources</h2><p className="mt-1 text-xs text-slate-500">{source.name ? "1 source ready for review" : "No source staged yet"}</p></div><Pill tone={source.name ? "green" : "neutral"}>{source.name ? "Staged" : "Empty"}</Pill></div>{source.name ? <div className="mt-5 flex items-center justify-between rounded-xl border border-slate-200 px-4 py-3"><div><p className="text-sm font-medium text-slate-800">{source.name}</p><p className="mt-1 text-xs text-slate-500">{source.kind} · digest generated by backend</p></div><span className="font-mono text-xs text-slate-400">pending</span></div> : null}</Panel>
              </div>
            ) : null}

            {activeTab === "review" ? (
              <div className="space-y-6">
                <Panel className="p-5 sm:p-7"><SectionHeading eyebrow="Capability plan" title="Choose the smallest useful surface." description="Every capability starts disabled. Save the explicit selection for owner review before it can appear in a generated plan." /><div>{capabilities.map((capability) => <Toggle key={capability.id} label={capability.label} description={capability.description} checked={Boolean(enabled[capability.id])} onChange={() => setEnabled((current) => ({ ...current, [capability.id]: !current[capability.id] }))} />)}</div><div className="mt-5 flex items-center justify-between border-t border-slate-100 pt-5"><p className="text-xs text-slate-500">{enabledCount} of {capabilities.length} capabilities enabled</p><button type="button" onClick={saveCapabilities} className="factory-button-primary">Save selection <Icon name="arrow" /></button></div></Panel>
                <Panel className="p-5 sm:p-7"><SectionHeading eyebrow="Plan digest" title="Confirm the exact review artifact." description="The digest represents the plan inputs and capability selection. Confirmation is deliberate and recorded in the immutable timeline." /><div className="rounded-xl border border-slate-200 bg-slate-50 p-4"><div className="flex items-center justify-between gap-4"><span className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">Plan digest</span><Pill tone="blue">Immutable</Pill></div><p className="mt-3 break-all font-mono text-sm text-slate-800">sha256:7b3f4e91c202…9c21</p><p className="mt-2 text-xs leading-5 text-slate-500">Inputs: company profile · {enabledCount} capability{enabledCount === 1 ? "" : "ies"} · staged source</p></div><div className="mt-4 grid gap-2 sm:grid-cols-2"><div className="flex items-center gap-2 rounded-lg border border-emerald-100 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-800"><Icon name="check" /> Knowledge approval bound to source digest</div><div className="flex items-center gap-2 rounded-lg border border-emerald-100 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-800"><Icon name="check" /> Template provenance block enforced</div></div><label className="mt-5 flex cursor-pointer items-start gap-3"><input type="checkbox" checked={digestConfirmed} onChange={(e) => setDigestConfirmed(e.target.checked)} className="mt-0.5 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500" /><span className="text-sm leading-6 text-slate-600">I reviewed this digest and confirm it is ready for a PR handoff.</span></label><button type="button" disabled={!canConfirmPlan} onClick={confirmPlan} className="factory-button-primary mt-5 disabled:cursor-not-allowed disabled:opacity-40">Confirm plan digest <Icon name="check" /></button></Panel>
              </div>
            ) : null}

            {activeTab === "handoff" ? (
              <div className="space-y-6">
                <Panel className="p-5 sm:p-7"><SectionHeading eyebrow="PR handoff" title="A review artifact, not a deployment switch." description="Generate the handoff packet for code review. Merging and deployment remain outside this console and require separate owner-controlled systems." /><div className="grid gap-4 sm:grid-cols-2"><div className="rounded-xl border border-slate-200 p-4"><p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-400">Generation</p><p className="mt-2 font-mono text-sm text-slate-800">factory-review / pending</p><p className="mt-1 text-xs text-slate-500">Immutable generation ID assigned by backend</p></div><div className="rounded-xl border border-slate-200 p-4"><p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-400">Checks</p><p className="mt-2 text-sm font-medium text-slate-800">Awaiting plan confirmation</p><p className="mt-1 text-xs text-slate-500">Knowledge digest · template provenance · secret scan</p></div></div><button type="button" onClick={openHandoff} className="factory-button-primary mt-6"><Icon name="external" /> Prepare review handoff</button></Panel>
                <Panel className="border-amber-200 bg-amber-50/50 p-5 sm:p-7"><div className="flex items-start gap-3"><div className="rounded-lg bg-amber-100 p-2 text-amber-700"><Icon name="lock" /></div><div><h2 className="text-sm font-semibold text-amber-950">Deployment controls are disabled</h2><p className="mt-1 text-sm leading-6 text-amber-900/70">Provisioning, DNS changes, provider setup, and production rollout are intentionally unavailable in the factory console.</p><div className="mt-4 flex flex-wrap gap-2"><button type="button" disabled className="factory-button-disabled">Deploy agent</button><button type="button" disabled className="factory-button-disabled">Provision infrastructure</button><button type="button" disabled className="factory-button-disabled">Configure DNS</button></div></div></div></Panel>
              </div>
            ) : null}
          </div>

          <aside className="space-y-6 lg:pt-[69px]"><Panel className="overflow-hidden"><div className="border-b border-slate-100 px-5 py-4"><div className="flex items-center justify-between"><h2 className="text-sm font-semibold text-slate-900">Immutable timeline</h2><Pill tone="blue">Append-only</Pill></div><p className="mt-1 text-xs text-slate-500">Owner actions and backend attestations</p></div><ol className="space-y-0 px-5 py-2">{timeline.map((event) => <li key={event.id} className="relative flex gap-3 py-4"><div className="relative flex w-4 shrink-0 justify-center"><span className={`z-10 mt-1 h-2.5 w-2.5 rounded-full ring-4 ring-white ${event.tone === "done" ? "bg-emerald-500" : event.tone === "current" ? "bg-cyan-500" : "bg-slate-300"}`} />{event.id !== timeline[timeline.length - 1].id ? <span className="absolute top-4 h-full w-px bg-slate-200" aria-hidden="true" /> : null}</div><div className="min-w-0"><p className="text-xs font-semibold text-slate-800">{event.title}</p><p className="mt-1 text-xs leading-5 text-slate-500">{event.detail}</p><p className="mt-1.5 text-[10px] font-medium uppercase tracking-[0.12em] text-slate-400">{event.time}</p></div></li>)}</ol></Panel><Panel className="p-5"><div className="flex items-start gap-3"><div className="rounded-lg bg-slate-100 p-2 text-slate-500"><Icon name="lock" /></div><div><h2 className="text-sm font-semibold text-slate-900">Boundary check</h2><p className="mt-1 text-xs leading-5 text-slate-500">Review-only factory. No provider tokens, infrastructure access, arbitrary fetches, or deployment actions.</p></div></div></Panel></aside>
        </div>
      </div>

      {pending ? <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 px-4 backdrop-blur-[2px]"><div role="dialog" aria-modal="true" aria-labelledby="confirmation-title" className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"><div className="flex items-start gap-3"><div className="rounded-lg bg-cyan-50 p-2 text-cyan-700"><Icon name="lock" /></div><div><p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-cyan-700">Owner confirmation</p><h2 id="confirmation-title" className="mt-1 text-xl font-semibold tracking-tight text-slate-950">{pending.title}?</h2></div></div><p className="mt-4 text-sm leading-6 text-slate-600">{pending.detail}</p><div className="mt-6 flex justify-end gap-3"><button type="button" onClick={() => setPending(null)} className="factory-button-secondary">Cancel</button><button type="button" onClick={confirmAction} className="factory-button-primary">Confirm action <Icon name="check" /></button></div></div></div> : null}
    </main>
  );
}
