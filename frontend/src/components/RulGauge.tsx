/** Semicircular remaining-life gauge: the arc band is the 90% interval, the needle the point estimate,
 *  the tick the true RUL (evaluation only). Scale 0–125 cycles (the C-MAPSS cap). */
export function RulGauge({ rul, low, high, truth, size = 210 }: { rul: number; low: number; high: number; truth?: number; size?: number }) {
  const R = 80, cx = 100, cy = 96, max = 125;
  const ang = (v: number) => Math.PI * (1 - Math.max(0, Math.min(max, v)) / max);
  const pt = (v: number, r = R) => [cx + r * Math.cos(ang(v)), cy - r * Math.sin(ang(v))];
  const arc = (a: number, b: number, r: number) => {
    const [x0, y0] = pt(a, r), [x1, y1] = pt(b, r);
    return `M ${x0} ${y0} A ${r} ${r} 0 0 1 ${x1} ${y1}`;
  };
  const [nx, ny] = pt(rul, R - 8);
  const tr = truth !== undefined ? pt(truth, R + 9) : null;
  const tr2 = truth !== undefined ? pt(truth, R - 9) : null;
  return (
    <svg viewBox="0 0 200 120" width={size} role="img" aria-label={`Remaining life ${rul} cycles, 90% range ${low} to ${high}`}>
      <path d={arc(0, max, R)} stroke="#ececf6" strokeWidth="16" fill="none" strokeLinecap="round" />
      <path d={arc(0, 30, R)} stroke="#b5b6d6" strokeWidth="16" fill="none" />
      <path d={arc(low, Math.max(low + 0.5, high), R)} stroke="#53558c" strokeWidth="16" fill="none" style={{ transition: "d .4s" }} />
      <line x1={cx} y1={cy} x2={nx} y2={ny} stroke="#2f3169" strokeWidth="3.5" strokeLinecap="round" style={{ transition: "all .35s" }} />
      <circle cx={cx} cy={cy} r="6" fill="#2f3169" />
      {tr && tr2 && <line x1={tr[0]} y1={tr[1]} x2={tr2[0]} y2={tr2[1]} stroke="#2f3169" strokeWidth="2" strokeDasharray="2 2" />}
      <text x="18" y="114" fontSize="9" fill="#53558c">0</text>
      <text x="170" y="114" fontSize="9" fill="#53558c">125</text>
      <text x="38" y="62" fontSize="8" fill="#53558c" transform="rotate(-62 38 62)">≤30 inspect</text>
    </svg>
  );
}
