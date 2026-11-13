import type { ReactNode } from "react";

/** Header row shared by every bento cell: a small uppercase label and an optional note on the right. */
export function TileHeader({ label, meta, children, metaClassName = "" }: {
  label: string;
  meta?: ReactNode;
  children?: ReactNode;
  metaClassName?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 px-5 pt-4">
      <div className="flex items-center gap-2.5">
        <span className="size-1.5 bg-ink-3" aria-hidden />
        <h2 className="whitespace-nowrap font-mono text-[10.5px] font-medium uppercase tracking-[0.18em] text-ink-3">{label}</h2>
        {children}
      </div>
      {meta != null && (
        <div className={`min-w-0 truncate whitespace-nowrap font-mono text-[10.5px] tracking-[0.04em] text-ink-3 ${metaClassName}`}>{meta}</div>
      )}
    </div>
  );
}

/** 24 hourly bars, oldest first; the current hour is drawn in full ink. */
export function HourBars({ hourly, height = 18, bar = 4, gap = 2, className = "" }: {
  hourly: number[];
  height?: number;
  bar?: number;
  gap?: number;
  className?: string;
}) {
  const max = Math.max(1, ...hourly);
  const bw = bar;
  const width = hourly.length * (bw + gap) - gap;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={`block shrink-0 ${className}`}
      shapeRendering="crispEdges"
      aria-hidden
    >
      {hourly.map((n, i) => {
        const h = n ? Math.max(2, Math.round((n / max) * height)) : 1;
        const tone = i === hourly.length - 1 && n ? "fill-ink" : n ? "fill-ink-3" : "fill-line-strong";
        return <rect key={i} x={i * (bw + gap)} y={height - h} width={bw} height={h} className={tone} />;
      })}
    </svg>
  );
}
