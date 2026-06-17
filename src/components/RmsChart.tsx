// Live RMS sparkline shown in the preview toolbar (left) on the same row as the
// action buttons — only mounted while a render is running (see PreviewPane). The
// current value sits ABOVE the "RMS" caption, with the sparkline beside it. RMS
// falls as shapes accumulate, so the curve descends left to right. Pure inline
// SVG, no charting dependency.
import { useMemo } from "react";
import type { RmsPoint } from "../types";

const W = 130; // sparkline viewBox width
const PLOT_H = 44; // sparkline viewBox height (matches the rendered px height)
const PAD = 5;

export default function RmsChart({ data }: { data: RmsPoint[] }) {
  const geom = useMemo(() => {
    if (data.length < 2) return null;
    const xs = data.map((d) => d.n);
    const ys = data.map((d) => d.rms);
    const xMin = xs[0];
    const xMax = xs[xs.length - 1];
    let yMin = Math.min(...ys);
    let yMax = Math.max(...ys);
    if (yMax - yMin < 1e-6) {
      // Flat series — pad so the line sits mid-row, not on an edge.
      yMax += 0.5;
      yMin -= 0.5;
    }
    const sx = (n: number) => PAD + ((n - xMin) / Math.max(1, xMax - xMin)) * (W - 2 * PAD);
    // High rms (early/worse) maps to the top, low rms (later/better) to the bottom.
    const sy = (r: number) => PAD + (1 - (r - yMin) / (yMax - yMin)) * (PLOT_H - 2 * PAD);
    const line = data.map((d, i) => `${i ? "L" : "M"}${sx(d.n).toFixed(1)},${sy(d.rms).toFixed(1)}`).join(" ");
    const last = data[data.length - 1];
    const area = `${line} L${sx(xMax).toFixed(1)},${PLOT_H - PAD} L${sx(xMin).toFixed(1)},${PLOT_H - PAD} Z`;
    return { line, area, cx: sx(last.n), cy: sy(last.rms) };
  }, [data]);

  if (!geom) return null;
  const cur = data[data.length - 1];
  return (
    <div className="rms-chart" role="img" aria-label={`RMS ${cur.rms.toFixed(2)}`}>
      <div className="rms-chart-readout">
        <span className="rms-chart-val">{cur.rms.toFixed(2)}</span>
        <span className="rms-chart-title">RMS</span>
      </div>
      <svg className="rms-chart-svg" viewBox={`0 0 ${W} ${PLOT_H}`} preserveAspectRatio="none">
        <path className="rms-chart-area" d={geom.area} />
        <path className="rms-chart-line" d={geom.line} />
        <circle className="rms-chart-dot" cx={geom.cx} cy={geom.cy} r={2.2} />
      </svg>
    </div>
  );
}
