import { useState } from "react";
import { AgentsPanel } from "./components/AgentsPanel";
import { BrainView } from "./components/Brain";
import { Header, ThemeToggle, useTab } from "./components/Chrome";
import { LibraryView } from "./components/Library";
import { LiveFeed } from "./components/LiveFeed";
import { NodeGraph } from "./components/NodeGraph";
import { StatsBento } from "./components/StatsBento";
import { TileHeader } from "./components/Tile";
import { freshness } from "./lib/format";
import { useActivityStream, useNow } from "./lib/stream";

export default function App() {
  const { state, subscribe } = useActivityStream();
  const now = useNow(state.skew);
  const [filter, setFilter] = useState<string | null>(null);
  const [tab, setTab] = useTab();
  const agents = state.order.map((id) => state.agents[id]);
  const syncing = agents.filter((a) => freshness(a.last_seen, now) === "live").length;

  return (
    <div className="min-h-dvh bg-bg text-ink">
      <div className="mx-auto max-w-[1480px] px-4 pb-28 pt-5 sm:px-6 lg:px-8 lg:pt-7">
        <Header status={state.status} retryIn={state.retryIn} tab={tab} onTab={setTab} />

        {tab === "library" ? (
          <LibraryView subscribe={subscribe} />
        ) : tab === "brain" ? (
          <BrainView subscribe={subscribe} />
        ) : (
        /* one hairline bento: the 1px gaps over a line-coloured backdrop draw every border */
        <main className="mt-5 grid grid-cols-12 gap-px border border-line bg-line">
          <StatsBento totals={state.totals} agents={agents} feed={state.feed} now={now} />

          <section className="col-span-12 bg-surface lg:col-span-8 xl:col-span-9">
            <TileHeader
              label="Topology"
              meta={`${agents.length} agents · ${syncing ? `${syncing} syncing` : "all quiet"}`}
            />
            {state.status === "denied" ? (
              <Denied message={state.denied} />
            ) : (
              <div className="px-2 pb-2 pt-1 sm:px-4">
                <NodeGraph agents={state.agents} order={state.order} memories={state.totals.memories} now={now} subscribe={subscribe} />
              </div>
            )}
            <Legend />
          </section>

          <section className="col-span-12 bg-surface lg:col-span-4 xl:col-span-3">
            <AgentsPanel agents={agents} now={now} filter={filter} onFilter={setFilter} />
          </section>

          <section className="col-span-12 bg-surface pb-1">
            <LiveFeed feed={state.feed} filter={filter} onClear={() => setFilter(null)} />
          </section>
        </main>
        )}
      </div>
      <ThemeToggle />
    </div>
  );
}

function Legend() {
  const items = [
    ["recall", "var(--recall)"],
    ["remember", "var(--remember)"],
    ["forget", "var(--forget)"],
    ["library", "var(--library)"],
    ["session · profile · timeline", "var(--context)"],
  ] as const;
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-1.5 border-t border-line px-5 py-3 font-mono text-[10.5px] text-ink-3">
      {items.map(([label, color]) => (
        <span key={label} className="flex items-center gap-2">
          <span className="size-1.5" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

function Denied({ message }: { message: string | null }) {
  return (
    <div className="px-5 py-16 text-[13.5px] text-ink-2">
      <p>{message ?? "This view is not available here."}</p>
      <p className="mt-2 text-ink-3">
        Open <code className="border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[12px] text-ink">https://memory.your-tailnet.ts.net:8420/live</code> on a device signed in to Tailscale.
      </p>
    </div>
  );
}
