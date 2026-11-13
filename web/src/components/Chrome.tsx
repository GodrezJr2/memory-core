import { motion } from "framer-motion";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTheme, type Theme } from "../lib/hooks";
import { IOS_SPRING } from "../lib/motion";
import type { StreamStatus } from "../lib/types";

export type Tab = "live" | "brain" | "library";

/** The open tab lives in the URL hash, so /live#library opens straight on the library (and Back works). */
export function useTab(): [Tab, (t: Tab) => void] {
  const read = (): Tab => (window.location.hash === "#library" ? "library" : window.location.hash === "#brain" ? "brain" : "live");
  const [tab, setTabState] = useState<Tab>(read);
  useEffect(() => {
    const onHash = () => setTabState(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const setTab = useCallback((t: Tab) => {
    window.location.hash = t === "live" ? "" : t;
    setTabState(t);
    window.scrollTo({ top: 0 });
  }, []);
  return [tab, setTab];
}

export function Header({ status, retryIn, tab, onTab }: {
  status: StreamStatus;
  retryIn: number | null;
  tab: Tab;
  onTab: (t: Tab) => void;
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-3">
        <Mark />
        <div>
          <h1 className="text-[15px] font-semibold tracking-[-0.015em]">
            memory-core <span className="font-normal text-ink-3">/ {tab}</span>
          </h1>
          <p className="font-mono text-[10.5px] tracking-[0.04em] text-ink-3">homeserver · shared agent memory</p>
        </div>
      </div>
      <div className="flex w-full items-center justify-between gap-3 sm:w-auto">
        <Tabs tab={tab} onTab={onTab} />
        <StatusPill status={status} retryIn={retryIn} />
      </div>
    </header>
  );
}

function Tabs({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const tabs: { value: Tab; label: string }[] = [
    { value: "live", label: "Live" },
    { value: "brain", label: "Brain" },
    { value: "library", label: "Library" },
  ];
  return (
    <nav aria-label="View" className="flex border border-line bg-surface p-0.5">
      {tabs.map((t) => {
        const on = t.value === tab;
        return (
          <button
            key={t.value}
            type="button"
            aria-current={on ? "page" : undefined}
            onClick={() => onTab(t.value)}
            className="relative h-7 px-3.5 font-mono text-[10.5px] uppercase tracking-[0.16em] outline-none focus-visible:ring-1 focus-visible:ring-ink"
          >
            {on && <motion.span layoutId="tab-thumb" className="absolute inset-0 bg-ink" transition={IOS_SPRING} />}
            <span className={`relative ${on ? "text-bg" : "text-ink-2"}`}>{t.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

function Mark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden shapeRendering="crispEdges">
      <rect x="0.5" y="0.5" width="25" height="25" fill="none" className="stroke-ink" />
      <rect x="9" y="9" width="8" height="8" style={{ fill: "var(--recall)" }} />
      <path d="M 0.5 13 H 9 M 17 13 H 25.5 M 13 0.5 V 9" className="stroke-ink-3" />
    </svg>
  );
}

const STATUS: Record<StreamStatus, { label: string; color: string; blink: boolean }> = {
  connecting: { label: "Connecting", color: "var(--ink-3)", blink: true },
  live: { label: "Live", color: "var(--recall)", blink: true },
  reconnecting: { label: "Reconnecting", color: "var(--error)", blink: false },
  denied: { label: "No access", color: "var(--error)", blink: false },
  demo: { label: "Demo data", color: "var(--remember)", blink: true },
};

function StatusPill({ status, retryIn }: { status: StreamStatus; retryIn: number | null }) {
  const s = STATUS[status];
  return (
    <div className="flex items-center gap-2 border border-line bg-surface px-2.5 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-ink-2">
      <motion.span
        className="size-1.5"
        style={{ background: s.color }}
        animate={s.blink ? { opacity: [1, 0.25, 1] } : { opacity: 1 }}
        transition={s.blink ? { duration: 1.6, repeat: Infinity, ease: "easeInOut" } : { duration: 0 }}
      />
      {s.label}
      {status === "reconnecting" && retryIn != null && <span className="text-ink-3">· {Math.round(retryIn / 1000)}s</span>}
    </div>
  );
}

/** Floating light/dark switch. The thumb springs across; the palette morphs underneath it. */
export function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  const options: { value: Theme; label: string; icon: ReactNode }[] = [
    { value: "light", label: "Light", icon: <SunIcon /> },
    { value: "dark", label: "Dark", icon: <MoonIcon /> },
  ];
  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="fixed bottom-5 right-5 z-50 flex border border-line-strong bg-surface/80 p-1 shadow-[0_12px_40px_-16px_rgba(0,0,0,0.45)] backdrop-blur-xl"
    >
      {options.map((o) => {
        const on = theme === o.value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => setTheme(o.value)}
            className="relative flex h-8 items-center px-3 font-mono text-[10.5px] uppercase tracking-[0.14em] outline-none focus-visible:ring-1 focus-visible:ring-ink"
          >
            {on && <motion.span layoutId="theme-thumb" className="absolute inset-0 bg-ink" transition={IOS_SPRING} />}
            <motion.span
              className="relative flex items-center gap-2"
              animate={{ color: on ? "var(--bg)" : "var(--ink-3)" }}
              transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
            >
              {o.icon}
              {o.label}
            </motion.span>
          </button>
        );
      })}
    </div>
  );
}

function SunIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden shapeRendering="crispEdges">
      <rect x="3.5" y="3.5" width="5" height="5" fill="none" stroke="currentColor" />
      <path d="M 6 0 V 2 M 6 10 V 12 M 0 6 H 2 M 10 6 H 12" stroke="currentColor" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden shapeRendering="crispEdges">
      <path d="M 1.5 1.5 H 10.5 V 10.5 H 1.5 Z" fill="none" stroke="currentColor" />
      <path d="M 1.5 1.5 L 10.5 10.5 V 1.5 Z" fill="currentColor" />
    </svg>
  );
}
