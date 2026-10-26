/** Small stroked icon set (24×24, currentColor). */
const P: Record<string, string> = {
  overview: "M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 7h6V4h-6z",
  learn: "M3 7l9-4 9 4-9 4-9-4zM7 9v5c0 1.5 2.2 3 5 3s5-1.5 5-3V9M21 7v6",
  change: "M4 7h11M12 4l3 3-3 3M20 17H9M12 14l-3 3 3 3",
  pdm: "M3 12h4l2-6 4 12 2-6h6",
  twin: "M2 12c3-4 7-4 10 0s7 4 10 0M6 9v6M12 7v10M18 9v6",
  sim: "M5 4l14 8-14 8z",
  safety: "M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z M9 12l2 2 4-4",
  quote: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h5",
  mro: "M12 8v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
  ap: "M4 6h16v12H4zM4 10h16M8 15h3",
  mission: "M12 2v4M12 18v4M2 12h4M18 12h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
  builder: "M4 5h6v5H4zM14 14h6v5h-6zM14 5h6v5h-6zM10 7.5h4M17 10v4",
  queue: "M4 6h16M4 12h16M4 18h10",
  approvals: "M5 12l4 4 10-10",
  obs: "M3 20h18M6 16V9M11 16V5M16 16v-5M21 16V8",
  evals: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9",
  data: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  research: "M4 4h12a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4zM8 9h8M8 13h6",
  play: "M7 4l13 8-13 8z",
  pause: "M7 4h4v16H7zM13 4h4v16h-4z",
  replay: "M4 4v6h6M20 12a8 8 0 1 1-2.3-5.7L20 9",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  check: "M5 12l4 4 10-10",
  x: "M6 6l12 12M18 6L6 18",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  flask: "M9 3h6M10 3v6l-6 10a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3L14 9V3",
  save: "M5 3h11l3 3v15H5zM8 3v6h7V3M8 21v-7h8v7",
  plus: "M12 5v14M5 12h14",
};

export function Icon({ name, size = 18, className = "ico", stroke = 1.8 }: { name: keyof typeof P | string; size?: number; className?: string; stroke?: number }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={P[name] ?? P.overview} />
    </svg>
  );
}

/** Brand mark: a revision cloud around a turbofan disc. */
export function BrandMark({ size = 34 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden="true">
      <path d="M8 12 a4 4 0 0 1 8-2 a4 4 0 0 1 8 0 a4 4 0 0 1 8 2 a4 4 0 0 1 1 8 a4 4 0 0 1-1 8 a4 4 0 0 1-8 2 a4 4 0 0 1-8 0 a4 4 0 0 1-8-2 a4 4 0 0 1-1-8 a4 4 0 0 1 1-8z"
            fill="none" stroke="#b5b6d6" strokeWidth="1.8" />
      <circle cx="20" cy="20" r="7.5" fill="none" stroke="#fff" strokeWidth="1.6" />
      {Array.from({ length: 8 }).map((_, i) => {
        const a = (i * Math.PI) / 4;
        return <path key={i} d={`M20 20 L${(20 + 7.2 * Math.cos(a + 0.5)).toFixed(2)} ${(20 + 7.2 * Math.sin(a + 0.5)).toFixed(2)}`}
                     stroke="#dedff1" strokeWidth="1.3" />;
      })}
      <circle cx="20" cy="20" r="2" fill="#fff" />
    </svg>
  );
}
