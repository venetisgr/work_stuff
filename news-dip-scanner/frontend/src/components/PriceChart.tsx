"use client";

/**
 * The idea's price chart: ~6 months of daily closes (one series, the accent colour, a light wash under it), the
 * idea's levels as dashed lines labelled in a gutter on the right (each line has its own dash pattern and a label,
 * so none is told apart by colour alone; the levels card is their legend), a marker at the report's day, and a
 * crosshair that snaps to the nearest close on hover, touch drag and the arrow keys. Drawn at the width it is shown,
 * so text stays 11.5px on a phone and a desktop. No inline styles: colours come from classes in globals.css.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import {
  LEVELS,
  areaPath,
  chartSummary,
  dayTime,
  layoutFor,
  linePath,
  linearScale,
  monthTicks,
  nearestIndex,
  niceTicks,
  spreadLabels,
  tickGutter,
  tickText,
  toPoints,
  yDomain,
} from "@/lib/chart";
import { formatDay, formatPct, formatPrice, formatShortDay } from "@/lib/format";
import type { ChartData } from "@/lib/types";

const DEFAULT_WIDTH = 358; // a 390px phone less the page's and card's padding: the first paint is mobile-first
const LABEL_GAP_WIDE = 15.5;
const LABEL_GAP_NARROW = 14;

export default function PriceChart({ data, name }: { data: ChartData; name: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [active, setActive] = useState<number | null>(null);
  const [announce, setAnnounce] = useState("");
  const id = useId();

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth || DEFAULT_WIDTH);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const layout = layoutFor(width);
  const points = useMemo(() => toPoints(data.closes), [data.closes]);
  const signalT = dayTime(data.signal_day);

  const geometry = useMemo(() => {
    const plotRight = layout.width - layout.right;
    const plotTop = layout.top;
    const plotBottom = layout.height - layout.bottom;
    const start = Math.min(points[0]?.t ?? signalT, signalT);
    const end = Math.max(points[points.length - 1]?.t ?? signalT, signalT);
    const levelValues = LEVELS.map((level) => data.levels[level.field]);
    const [lo, hi] = yDomain([...points.map((p) => p.close), ...levelValues]);
    const ticks = niceTicks(lo, hi, layout.wide ? 7 : 6).filter((v) => v >= lo && v <= hi);
    const step = ticks.length > 1 ? ticks[1] - ticks[0] : hi - lo;
    const plotLeft = tickGutter(
      ticks.map((value) => tickText(value, step, data.currency)),
      layout.left,
    );
    const x = linearScale([start, end], [plotLeft, plotRight]);
    const y = linearScale([lo, hi], [plotBottom, plotTop]);
    const months = monthTicks(start, end, layout.wide ? 8 : 4);
    const last = points[points.length - 1];
    const labels = spreadLabels(
      [
        ...LEVELS.map((level) => ({ key: level.key as string, y: y(data.levels[level.field]) })),
        ...(last ? [{ key: "last", y: y(last.close) }] : []),
      ],
      layout.wide ? LABEL_GAP_WIDE : LABEL_GAP_NARROW,
      plotTop + 4,
      plotBottom - 4,
    );
    return { plotLeft, plotRight, plotTop, plotBottom, x, y, ticks, step, months, labels, last };
  }, [layout, points, data.levels, data.currency, signalT]);

  const { plotLeft, plotRight, plotTop, plotBottom, x, y } = geometry;
  const money = useCallback((value: number) => formatPrice(value, data.currency), [data.currency]);
  const summary = useMemo(() => chartSummary(data, name), [data, name]);

  const pick = useCallback(
    (clientX: number, target: SVGSVGElement) => {
      const box = target.getBoundingClientRect();
      const scale = layout.width / box.width;
      const px = (clientX - box.left) * scale;
      if (px < plotLeft - 8 || px > plotRight + 8) return;
      const span = plotRight - plotLeft;
      const start = points[0]?.t ?? 0;
      const end = Math.max(points[points.length - 1]?.t ?? 0, signalT);
      const t = start + ((px - plotLeft) / span) * (end - start);
      setActive(nearestIndex(points, t));
    },
    [layout.width, plotLeft, plotRight, points, signalT],
  );

  const onPointer = (event: PointerEvent<SVGSVGElement>) => pick(event.clientX, event.currentTarget);

  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (!points.length) return;
    const current = active ?? points.length - 1;
    const moves: Record<string, number> = {
      ArrowLeft: current - 1,
      ArrowRight: current + 1,
      PageUp: current - 5,
      PageDown: current + 5,
      Home: 0,
      End: points.length - 1,
    };
    if (event.key === "Escape") {
      setActive(null);
      return;
    }
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = Math.min(points.length - 1, Math.max(0, moves[event.key]));
    setActive(next);
    setAnnounce(`${formatDay(points[next].day)}: ${money(points[next].close)}`);
  };

  const point = active !== null ? points[active] : null;
  const labelText = (key: string): { name: string; value: string } => {
    if (key === "last" && geometry.last) return { name: "Last", value: money(geometry.last.close) };
    const level = LEVELS.find((item) => item.key === key)!;
    return { name: level.name, value: money(data.levels[level.field]) };
  };

  return (
    <div ref={frame} className="chart-frame relative">
      <svg
        className="chart-svg"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        width={layout.width}
        height={layout.height}
        role="img"
        aria-labelledby={`${id}-title ${id}-desc`}
        tabIndex={0}
        onPointerMove={onPointer}
        onPointerDown={onPointer}
        onPointerLeave={(event) => {
          if (event.pointerType === "mouse") setActive(null);
        }}
        onKeyDown={onKey}
        onBlur={() => setActive(null)}
      >
        <title id={`${id}-title`}>{`${name}: daily closes and the idea's levels`}</title>
        <desc id={`${id}-desc`}>{summary}</desc>

        {/* grid and price axis */}
        {geometry.ticks.map((value) => (
          <g key={value}>
            <line className="chart-grid" x1={plotLeft} x2={plotRight} y1={y(value)} y2={y(value)} />
            <text className="chart-tick" x={plotLeft - 8} y={y(value)} dy="0.32em" textAnchor="end">
              {tickText(value, geometry.step, data.currency)}
            </text>
          </g>
        ))}
        <line className="chart-baseline" x1={plotLeft} x2={plotRight} y1={plotBottom} y2={plotBottom} />
        {geometry.months.map((tick) => (
          <g key={tick.t}>
            <line className="chart-baseline" x1={x(tick.t)} x2={x(tick.t)} y1={plotBottom} y2={plotBottom + 4} />
            <text className="chart-tick" x={x(tick.t)} y={plotBottom + 17} textAnchor="middle">
              {tick.label}
            </text>
          </g>
        ))}

        {/* the closes */}
        <path className="chart-area" d={areaPath(points, x, y, plotBottom)} />

        {/* the levels, each with its label in the right-hand gutter */}
        {LEVELS.map((level) => {
          const value = data.levels[level.field];
          const slot = geometry.labels.find((item) => item.key === level.key);
          const ly = y(value);
          return (
            <g key={level.key}>
              <line className={`chart-level chart-level-${level.key}`} x1={plotLeft} x2={plotRight} y1={ly} y2={ly} />
              {slot && Math.abs(slot.labelY - ly) > 0.5 ? (
                <path
                  className={`chart-level chart-leader chart-level-${level.key}`}
                  d={`M${plotRight},${ly}L${plotRight + 6},${ly}L${plotRight + 10},${slot.labelY}L${plotRight + 12},${slot.labelY}`}
                />
              ) : (
                <line className={`chart-level chart-leader chart-level-${level.key}`} x1={plotRight} x2={plotRight + 12} y1={ly} y2={ly} />
              )}
            </g>
          );
        })}
        <path className="chart-line" d={linePath(points, x, y)} />

        {/* the report's day */}
        <line className="chart-marker-line" x1={x(signalT)} x2={x(signalT)} y1={plotTop - 6} y2={plotBottom} />
        <text
          className="chart-marker-label"
          x={x(signalT)}
          y={plotTop - 10}
          textAnchor={x(signalT) > plotRight - 60 ? "end" : x(signalT) < plotLeft + 60 ? "start" : "middle"}
        >
          {`Reported ${formatShortDay(data.signal_day)}`}
        </text>
        <circle className="chart-marker-dot" cx={x(signalT)} cy={y(data.levels.reported)} r={4.5} />

        {/* the last close */}
        {geometry.last ? <circle className="chart-dot" cx={x(geometry.last.t)} cy={y(geometry.last.close)} r={4} /> : null}

        {/* labels */}
        {geometry.labels.map((slot) => {
          const text = labelText(slot.key);
          return (
            <text key={slot.key} x={plotRight + 14} y={slot.labelY} dy="0.32em">
              <tspan className="chart-label-name">{text.name}</tspan>
              {layout.wide ? (
                <tspan className="chart-label-value" dx={5}>
                  {text.value}
                </tspan>
              ) : null}
            </text>
          );
        })}

        {/* hover: crosshair, dot and the close */}
        {point ? <Crosshair point={point} x={x} y={y} top={plotTop} bottom={plotBottom} left={plotLeft} right={plotRight} money={money} reported={data.levels.reported} signalT={signalT} /> : null}
        <rect className="chart-hit" x={plotLeft} y={plotTop} width={plotRight - plotLeft} height={plotBottom - plotTop} />
      </svg>
      <p className="visually-hidden" aria-live="polite">
        {announce}
      </p>
    </div>
  );
}

function Crosshair({
  point,
  x,
  y,
  top,
  bottom,
  left,
  right,
  money,
  reported,
  signalT,
}: {
  point: { day: string; t: number; close: number };
  x: (v: number) => number;
  y: (v: number) => number;
  top: number;
  bottom: number;
  left: number;
  right: number;
  money: (v: number) => string;
  reported: number;
  signalT: number;
}) {
  const px = x(point.t);
  const py = y(point.close);
  const value = money(point.close);
  const day = formatDay(point.day);
  const change = point.t >= signalT ? `${formatPct((point.close / reported - 1) * 100)} vs reported` : null;
  const lines = [day, change].filter(Boolean) as string[];
  const width = Math.max(value.length * 8.6, ...lines.map((line) => line.length * 6.7)) + 20;
  const height = 24 + lines.length * 16;
  const flip = px + 12 + width > right;
  const bx = flip ? Math.max(left, px - 12 - width) : px + 12;
  const by = Math.min(Math.max(top, py - height - 10), bottom - height);
  return (
    <g pointerEvents="none">
      <line className="chart-crosshair" x1={px} x2={px} y1={top} y2={bottom} />
      <circle className="chart-dot" cx={px} cy={py} r={5} />
      <g transform={`translate(${bx.toFixed(1)},${by.toFixed(1)})`}>
        <rect className="chart-tip-box" width={width} height={height} rx={8} />
        <text className="chart-tip-value" x={10} y={20}>
          {value}
        </text>
        {lines.map((line, index) => (
          <text key={line} className="chart-tip-day" x={10} y={38 + index * 16}>
            {line}
          </text>
        ))}
      </g>
    </g>
  );
}
