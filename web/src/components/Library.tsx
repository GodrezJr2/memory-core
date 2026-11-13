import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent, type ReactNode } from "react";
import {
  capture,
  deleteRef,
  getRef,
  hostOf,
  listRefs,
  mediaUrl,
  prettyDate,
  shareGroup,
  toJpegBase64,
  type RefCard,
  type RefDetail,
  type RefList,
  type RefStatus,
} from "../lib/library";
import { EASE_IOS, IOS_SPRING } from "../lib/motion";
import type { CallEvent } from "../lib/types";
import { TileHeader } from "./Tile";

type Filter = RefStatus | "all";
const STATUS_COLOR: Record<RefStatus, string> = { inbox: "var(--remember)", reviewed: "var(--recall)" };

/** The Library tab: add screenshots and links, and browse what is saved, inbox and reviewed. */
export function LibraryView({ subscribe }: { subscribe: (fn: (e: CallEvent) => void) => () => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [data, setData] = useState<RefList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    const ctrl = new AbortController();
    const t = window.setTimeout(
      () =>
        listRefs(filter, query, ctrl.signal)
          .then((d) => {
            setData(d);
            setError(null);
          })
          .catch((e: Error) => e.name !== "AbortError" && setError(e.message)),
      query ? 300 : 0,
    );
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [filter, query, version]);

  // A review done by an agent elsewhere (or a capture from another device) shows up here at once.
  useEffect(
    () =>
      subscribe((e) => {
        if (e.action === "capture_ref" || e.action === "library_review") refresh();
      }),
    [subscribe, refresh],
  );

  return (
    <main className="mt-5 grid grid-cols-12 gap-px border border-line bg-line">
      <section className="col-span-12 bg-surface lg:col-span-4">
        <Composer onSaved={refresh} />
      </section>
      <section className="col-span-12 min-h-[60dvh] overflow-hidden bg-surface lg:col-span-8">
        <TileHeader
          label="Saved"
          meta={data ? `${data.counts.inbox} in inbox · ${data.counts.reviewed} reviewed` : "loading"}
        />
        <div className="flex flex-col gap-3 px-5 pb-4 pt-3 sm:flex-row sm:items-center sm:justify-between">
          <Segmented
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "All" },
              { value: "inbox", label: "Inbox", count: data?.counts.inbox },
              { value: "reviewed", label: "Reviewed", count: data?.counts.reviewed },
            ]}
          />
          <SearchBox value={query} onChange={setQuery} />
        </div>
        {error ? (
          <p className="border-t border-line px-5 py-10 text-[13px] text-ink-2">{error}</p>
        ) : !data ? (
          <GridSkeleton />
        ) : data.refs.length === 0 ? (
          <Empty filter={filter} query={query} />
        ) : (
          <Grid refs={data.refs} onOpen={setOpen} />
        )}
      </section>
      <AnimatePresence>
        {open != null && (
          <DetailSheet
            key={open}
            id={open}
            onClose={() => setOpen(null)}
            onDeleted={() => {
              setOpen(null);
              refresh();
            }}
          />
        )}
      </AnimatePresence>
    </main>
  );
}

// --- adding ----------------------------------------------------------------------------------

interface Picked {
  file: File;
  preview: string;
}

function Composer({ onSaved }: { onSaved: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<Picked[]>([]);
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<{ done: number; total: number } | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [dragging, setDragging] = useState(false);

  const add = useCallback((files: File[]) => {
    const images = files.filter((f) => f.type.startsWith("image/") || /\.(heic|heif)$/i.test(f.name));
    if (!images.length) return;
    setResult(null);
    setPicked((p) => [...p, ...images.map((file) => ({ file, preview: URL.createObjectURL(file) }))].slice(0, 10));
  }, []);

  // Paste a screenshot straight from the clipboard (desktop).
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length) {
        e.preventDefault();
        add(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [add]);

  const remove = (i: number) =>
    setPicked((p) => {
      URL.revokeObjectURL(p[i].preview);
      return p.filter((_, j) => j !== i);
    });

  const canSave = !busy && (picked.length > 0 || /^https?:\/\/\S+/.test(url.trim()));

  async function save() {
    if (!canSave) return;
    const group = shareGroup();
    const total = Math.max(1, picked.length);
    setBusy({ done: 0, total });
    setResult(null);
    try {
      let last = null;
      if (picked.length === 0) {
        last = await capture({ url: url.trim(), note: note.trim(), group });
      }
      for (let i = 0; i < picked.length; i++) {
        const image = await toJpegBase64(picked[i].file);
        last = await capture({ image, url: url.trim(), note: note.trim(), group });
        setBusy({ done: i + 1, total });
      }
      picked.forEach((p) => URL.revokeObjectURL(p.preview));
      setPicked([]);
      setUrl("");
      setNote("");
      setResult({ ok: true, text: last?.message ?? "Saved." });
      onSaved();
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    add(Array.from(e.dataTransfer.files));
  };

  return (
    <div>
      <TileHeader label="Add reference" meta="screenshot · link · note" />
      <div className="flex flex-col gap-3 px-5 pb-5 pt-3">
        <input
          ref={input}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e: ChangeEvent<HTMLInputElement>) => {
            add(Array.from(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`group flex min-h-28 flex-col items-center justify-center gap-2 border border-dashed px-4 py-6 text-center outline-none transition-colors focus-visible:border-ink ${
            dragging ? "border-ink bg-surface-2" : "border-line-strong hover:bg-surface-2"
          }`}
        >
          <PlusIcon />
          <span className="text-[13.5px] font-medium text-ink">Choose screenshots</span>
          <span className="font-mono text-[10.5px] tracking-[0.04em] text-ink-3">photos · drop · or paste</span>
        </button>

        <AnimatePresence initial={false}>
          {picked.length > 0 && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.35, ease: EASE_IOS }}
              className="overflow-hidden"
            >
              <div className="flex flex-wrap gap-2">
                {picked.map((p, i) => (
                  <div key={p.preview} className="relative size-20 border border-line bg-surface-2">
                    <img src={p.preview} alt="" className="size-full object-cover object-top" />
                    <button
                      type="button"
                      onClick={() => remove(i)}
                      aria-label="Remove image"
                      className="absolute right-0 top-0 flex size-6 items-center justify-center bg-ink/80 font-mono text-[12px] text-bg"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <Field label="Link (optional)">
          <input
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://x.com/… or threads.net/…"
            className="w-full border border-line bg-bg px-3 py-2.5 font-mono text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-ink"
          />
        </Field>
        <Field label="Why save this? (optional)">
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="e.g. Claude Code hook workflow to try"
            className="w-full resize-none border border-line bg-bg px-3 py-2.5 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-ink"
          />
        </Field>

        <button
          type="button"
          onClick={save}
          disabled={!canSave}
          className="relative h-11 overflow-hidden bg-ink font-mono text-[11px] uppercase tracking-[0.16em] text-bg outline-none transition-opacity disabled:opacity-35 focus-visible:ring-2 focus-visible:ring-ink focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
        >
          {busy && (
            <motion.span
              className="absolute inset-y-0 left-0 bg-[var(--library)]"
              initial={{ width: 0 }}
              animate={{ width: `${(busy.done / busy.total) * 100}%` }}
              transition={IOS_SPRING}
            />
          )}
          <span className="relative">
            {busy
              ? `Saving ${Math.min(busy.done + 1, busy.total)} / ${busy.total}…`
              : picked.length
                ? `Save ${picked.length} image${picked.length === 1 ? "" : "s"}`
                : "Save link"}
          </span>
        </button>
        <AnimatePresence>
          {result && (
            <motion.p
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex items-start gap-2 text-[12.5px] text-ink-2"
            >
              <span className="mt-1.5 size-1.5 shrink-0" style={{ background: result.ok ? "var(--recall)" : "var(--error)" }} />
              {result.text}
            </motion.p>
          )}
        </AnimatePresence>
        <p className="font-mono text-[10.5px] leading-relaxed tracking-[0.02em] text-ink-3">
          Text in images is read on arrival, so they are searchable at once. Ask Claude to “review my inbox” to get
          titles, descriptions and categories.
        </p>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-ink-3">{label}</span>
      {children}
    </label>
  );
}

// --- browsing --------------------------------------------------------------------------------

function Segmented({ value, onChange, options }: {
  value: Filter;
  onChange: (v: Filter) => void;
  options: { value: Filter; label: string; count?: number }[];
}) {
  return (
    <div role="radiogroup" aria-label="Show" className="flex border border-line p-0.5">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            className="relative flex h-8 flex-1 items-center justify-center gap-1.5 px-3 font-mono text-[10.5px] uppercase tracking-[0.14em] outline-none focus-visible:ring-1 focus-visible:ring-ink sm:flex-none"
          >
            {on && <motion.span layoutId="lib-filter" className="absolute inset-0 bg-ink" transition={IOS_SPRING} />}
            <span className={`relative ${on ? "text-bg" : "text-ink-2"}`}>{o.label}</span>
            {o.count != null && <span className={`relative ${on ? "text-bg/70" : "text-ink-3"}`}>{o.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

function SearchBox({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex h-9 items-center gap-2 border border-line bg-bg px-3 focus-within:border-ink sm:w-64">
      <SearchIcon />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search text, notes…"
        className="w-full bg-transparent text-[13.5px] text-ink outline-none placeholder:text-ink-3"
      />
    </label>
  );
}

function Grid({ refs, onOpen }: { refs: RefCard[]; onOpen: (id: number) => void }) {
  return (
    <div className="-mr-px grid grid-cols-2 border-t border-line sm:grid-cols-3 xl:grid-cols-4">
      {refs.map((r, i) => (
        <motion.button
          key={r.id}
          type="button"
          onClick={() => onOpen(r.id)}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: EASE_IOS, delay: Math.min(i, 12) * 0.03 }}
          className="group flex flex-col border-b border-r border-line bg-surface text-left outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ink"
        >
          <div className="relative aspect-[4/5] overflow-hidden bg-surface-2">
            {r.images.length ? (
              <img
                src={mediaUrl(r.images[0].sha256, "thumb")}
                alt=""
                loading="lazy"
                className="size-full object-cover object-top transition-transform duration-700 ease-[cubic-bezier(0.16,1,0.3,1)] group-hover:scale-[1.03]"
              />
            ) : (
              <div className="flex size-full flex-col items-center justify-center gap-2 px-3 text-center">
                <LinkIcon />
                <span className="break-all font-mono text-[11px] text-ink-2">{hostOf(r.url)}</span>
              </div>
            )}
            {r.images.length > 1 && (
              <span className="absolute right-2 top-2 bg-ink/80 px-1.5 py-0.5 font-mono text-[10px] text-bg">
                {r.images.length}
              </span>
            )}
          </div>
          <div className="flex flex-1 flex-col gap-1.5 p-3">
            <StatusTag status={r.status} />
            <p className="line-clamp-2 text-[13px] font-medium leading-snug text-ink">{r.title}</p>
            <p className="mt-auto truncate font-mono text-[10.5px] text-ink-3">
              {prettyDate(r.saved)}
              {r.category && ` · ${r.category}`}
            </p>
          </div>
        </motion.button>
      ))}
    </div>
  );
}

function StatusTag({ status }: { status: RefStatus }) {
  return (
    <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.16em] text-ink-3">
      <span className="size-1.5" style={{ background: STATUS_COLOR[status] }} />
      {status}
    </span>
  );
}

function GridSkeleton() {
  return (
    <div className="-mr-px grid grid-cols-2 border-t border-line sm:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="border-b border-r border-line bg-surface">
          <motion.div
            className="aspect-[4/5] bg-surface-2"
            animate={{ opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 1.6, repeat: Infinity, delay: i * 0.1 }}
          />
          <div className="h-16" />
        </div>
      ))}
    </div>
  );
}

function Empty({ filter, query }: { filter: Filter; query: string }) {
  const text = query
    ? `Nothing saved matches “${query}”.`
    : filter === "inbox"
      ? "Inbox is empty: everything saved has been reviewed."
      : filter === "reviewed"
        ? "Nothing reviewed yet. Ask Claude to “review my inbox”."
        : "Nothing saved yet. Add a screenshot or a link.";
  return <p className="border-t border-line px-5 py-16 text-center text-[13.5px] text-ink-2">{text}</p>;
}

// --- one reference -----------------------------------------------------------------------------

function DetailSheet({ id, onClose, onDeleted }: { id: number; onClose: () => void; onDeleted: () => void }) {
  const [ref, setRef] = useState<RefDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    getRef(id).then(setRef, (e: Error) => setError(e.message));
  }, [id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  useEffect(() => {
    if (!confirm) return;
    const t = window.setTimeout(() => setConfirm(false), 3500);
    return () => window.clearTimeout(t);
  }, [confirm]);

  async function remove() {
    if (!confirm) {
      setConfirm(true);
      return;
    }
    setDeleting(true);
    try {
      await deleteRef(id);
      onDeleted();
    } catch (e) {
      setError((e as Error).message);
      setDeleting(false);
    }
  }

  return (
    <motion.div className="fixed inset-0 z-[60] flex items-end justify-center sm:items-center sm:p-6" role="dialog" aria-modal>
      <motion.div
        className="absolute inset-0 bg-black/45 backdrop-blur-[2px]"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      />
      <motion.div
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={IOS_SPRING}
        className="relative flex max-h-[92dvh] w-full flex-col border border-line-strong bg-surface sm:max-w-3xl"
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          {ref ? <StatusTag status={ref.status} /> : <span />}
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-ink-2 outline-none hover:text-ink focus-visible:underline"
          >
            Close
          </button>
        </div>
        <div className="overflow-y-auto overscroll-contain pb-[env(safe-area-inset-bottom)]">
          {error && <p className="px-5 py-8 text-[13px] text-ink-2">{error}</p>}
          {!ref && !error && <div className="h-64" />}
          {ref && (
            <>
              {ref.images.length > 0 && (
                <div className="flex snap-x snap-mandatory gap-px overflow-x-auto bg-line">
                  {ref.images.map((img, i) => (
                    <a
                      key={img.sha256}
                      href={mediaUrl(img.sha256, "full")}
                      target="_blank"
                      rel="noreferrer"
                      className="relative w-full shrink-0 snap-center bg-surface-2"
                    >
                      <img
                        src={mediaUrl(img.sha256, "view")}
                        alt={`Image ${i + 1}`}
                        className="mx-auto max-h-[58dvh] w-auto object-contain"
                      />
                      {ref.images.length > 1 && (
                        <span className="absolute bottom-2 right-2 bg-ink/80 px-1.5 py-0.5 font-mono text-[10px] text-bg">
                          {i + 1} / {ref.images.length}
                        </span>
                      )}
                    </a>
                  ))}
                </div>
              )}
              <div className="flex flex-col gap-4 px-5 py-5">
                <div>
                  <h2 className="text-[18px] font-semibold leading-snug tracking-[-0.015em]">{ref.title}</h2>
                  <p className="mt-1.5 font-mono text-[10.5px] text-ink-3">
                    saved {prettyDate(ref.saved)}
                    {ref.reviewed && ` · reviewed ${prettyDate(ref.reviewed)}`}
                    {ref.category && ` · ${ref.category}${ref.category_auto ? " (guess)" : ""}`}
                  </p>
                </div>
                {ref.description ? (
                  <Block label="Review">{ref.description}</Block>
                ) : (
                  <p className="border border-dashed border-line-strong px-3 py-2.5 text-[13px] text-ink-2">
                    Waiting for review. Ask Claude: “review my inbox”.
                  </p>
                )}
                {ref.note && <Block label="Your note">{ref.note}</Block>}
                {ref.url && (
                  <Block label="Link">
                    <a href={ref.url} target="_blank" rel="noreferrer" className="break-all underline decoration-line-strong underline-offset-4 hover:decoration-ink">
                      {ref.url}
                    </a>
                  </Block>
                )}
                {ref.ocr.some(Boolean) && (
                  <details className="group border border-line">
                    <summary className="cursor-pointer list-none px-3 py-2.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-ink-3 outline-none hover:text-ink">
                      Text read from {ref.images.length > 1 ? "the images" : "the image"}
                    </summary>
                    <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-line px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-ink-2">
                      {ref.ocr.filter(Boolean).join("\n\n— — —\n\n")}
                    </pre>
                  </details>
                )}
                <button
                  type="button"
                  onClick={remove}
                  disabled={deleting}
                  className={`h-10 self-start border px-4 font-mono text-[10.5px] uppercase tracking-[0.16em] outline-none transition-colors focus-visible:ring-1 focus-visible:ring-ink ${
                    confirm ? "border-[var(--error)] bg-[var(--error)] text-white" : "border-line text-ink-2 hover:border-ink hover:text-ink"
                  }`}
                >
                  {deleting ? "Deleting…" : confirm ? "Tap again to delete" : "Delete"}
                </button>
              </div>
            </>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}

function Block({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-ink-3">{label}</p>
      <div className="mt-1.5 text-[14px] leading-relaxed text-ink">{children}</div>
    </div>
  );
}

// --- icons (square geometry, like the rest of the page) ------------------------------------------

function PlusIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden shapeRendering="crispEdges" className="text-ink">
      <rect x="0.5" y="0.5" width="21" height="21" fill="none" stroke="currentColor" />
      <path d="M11 6v10M6 11h10" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden className="shrink-0 text-ink-3" shapeRendering="crispEdges">
      <rect x="1.5" y="1.5" width="8" height="8" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M9 9l3.5 3.5" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden className="text-ink-3" shapeRendering="crispEdges">
      <rect x="2.5" y="7.5" width="8" height="5" fill="none" stroke="currentColor" />
      <rect x="9.5" y="7.5" width="8" height="5" fill="none" stroke="currentColor" />
    </svg>
  );
}
