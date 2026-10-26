/**
 * Revision cloud: the scalloped outline engineers draw around a change on a drawing.
 * It is the app's one visual signature — every value the AI wants a person to look at gets one.
 */
export function cloudPath(x: number, y: number, w: number, h: number, bump = 6): string {
  const pad = bump * 0.9;
  const x0 = x - pad, y0 = y - pad, x1 = x + w + pad, y1 = y + h + pad;
  const edge = (ax: number, ay: number, bx: number, by: number) => {
    const len = Math.hypot(bx - ax, by - ay);
    const n = Math.max(1, Math.round(len / (bump * 2)));
    let d = "";
    for (let i = 1; i <= n; i++) {
      const px = ax + ((bx - ax) * i) / n;
      const py = ay + ((by - ay) * i) / n;
      const r = len / n / 2;
      d += ` A ${r.toFixed(2)} ${r.toFixed(2)} 0 0 1 ${px.toFixed(2)} ${py.toFixed(2)}`;
    }
    return d;
  };
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)}${edge(x0, y0, x1, y0)}${edge(x1, y0, x1, y1)}${edge(x1, y1, x0, y1)}${edge(x0, y1, x0, y0)} Z`;
}

export function unionBox(boxes: Array<[number, number, number, number]>): [number, number, number, number] | null {
  if (!boxes.length) return null;
  const x0 = Math.min(...boxes.map((b) => b[0]));
  const y0 = Math.min(...boxes.map((b) => b[1]));
  const x1 = Math.max(...boxes.map((b) => b[0] + b[2]));
  const y1 = Math.max(...boxes.map((b) => b[1] + b[3]));
  return [x0, y0, x1 - x0, y1 - y0];
}

export const pct = (x: number, d = 1) => `${(x * 100).toFixed(d)}%`;
export const money = (x: number, cur = "CAD") =>
  new Intl.NumberFormat("en-CA", { style: "currency", currency: cur, maximumFractionDigits: 2 }).format(x);
export const label = (s: string) => s.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
