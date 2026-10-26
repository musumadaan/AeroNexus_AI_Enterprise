import { useMemo } from "react";
import type { FieldResult, SheetData } from "../api";
import { cloudPath, label, unionBox } from "../lib/cloud";

interface Props {
  sheet: SheetData;
  fields?: Record<string, FieldResult>;
  focus?: string | null;              // field name to emphasise
  highlightTokens?: string[];         // extra tokens to cloud (diffs, chat instances)
  onSelect?: (field: string) => void;
  caption?: string;
}

/** Renders OCR tokens on a drafting sheet. AI output is marked the way an engineer marks a drawing:
 *  values that need a person get a red revision cloud; accepted values get a faint ink wash. */
export function SheetView({ sheet, fields = {}, focus, highlightTokens = [], onSelect, caption }: Props) {
  const tokById = useMemo(() => new Map(sheet.tokens.map((t) => [t.id, t])), [sheet]);
  const marks = Object.values(fields)
    .filter((f) => f.value !== null && f.token_ids.length)
    .map((f) => ({ f, box: unionBox(f.token_ids.map((id) => tokById.get(id)!.bbox).filter(Boolean)) }))
    .filter((m) => m.box);
  const hl = unionBox(highlightTokens.map((id) => tokById.get(id)?.bbox).filter(Boolean) as [number, number, number, number][]);
  const H = sheet.height;

  const hasHoles = sheet.tokens.some((t) => t.text.startsWith("Ø"));
  const zoomBox = hl ?? (focus ? marks.find((m) => m.f.name === focus)?.box ?? null : null);

  const content = (
    <>
      <defs>
        <pattern id={`grid-${sheet.id}`} width="20" height="20" patternUnits="userSpaceOnUse">
          <path d="M 20 0 L 0 0 0 20" fill="none" stroke="var(--grid)" strokeWidth="0.6" />
        </pattern>
      </defs>
      <rect x={-10} y={-10} width={sheet.width + 20} height={H + 20} fill={`url(#grid-${sheet.id})`} />
      <rect x={8} y={8} width={sheet.width - 16} height={H - 16} fill="none" stroke="var(--ink)" strokeWidth="1.4" />
      {/* schematic part view: plate outline, holes and a dimension line, so the sheet reads as a drawing */}
      <g stroke="var(--ink)" fill="none" strokeWidth="1.1" opacity="0.75">
        <rect x={110} y={190} width={340} height={200} />
        <line x1={110} y1={176} x2={450} y2={176} strokeWidth="0.6" />
        <path d="M110 176 l8 -3 v6 z M450 176 l-8 -3 v6 z" fill="var(--ink)" strokeWidth="0" />
        <line x1={110} y1={170} x2={110} y2={190} strokeWidth="0.5" /><line x1={450} y1={170} x2={450} y2={190} strokeWidth="0.5" />
        {hasHoles && [[150, 230], [410, 230], [150, 350], [410, 350]].map(([cx, cy]) => (
          <g key={`${cx}-${cy}`}><circle cx={cx} cy={cy} r={11} /><path d={`M${cx - 16} ${cy} h32 M${cx} ${cy - 16} v32`} strokeWidth="0.4" strokeDasharray="5 2 1 2" /></g>
        ))}
        <path d="M280 190 v200" strokeWidth="0.4" strokeDasharray="10 3 2 3" />
      </g>
      {(sheet.regions ?? []).filter((r) => r.label !== "view").map((r) => (
        <g key={r.label}>
          <rect x={r.bbox[0] - 8} y={r.bbox[1] - 8} width={r.bbox[2] + 16} height={r.bbox[3] + 16}
                fill="none" stroke="var(--graphite)" strokeDasharray="4 3" strokeWidth="0.8" />
          <text x={r.bbox[0] - 6} y={r.bbox[1] - 12} fontSize="9" fill="var(--graphite)">{label(r.label)}</text>
        </g>
      ))}
      {marks.filter((m) => m.f.status === "auto_accepted").map(({ f, box }) => (
        <rect key={f.name} x={box![0] - 2} y={box![1] - 1} width={box![2] + 4} height={box![3] + 3}
              fill="rgba(31,36,48,.09)" rx={2} style={{ cursor: onSelect ? "pointer" : "default" }}
              onClick={() => onSelect?.(f.name)} />
      ))}
      {sheet.tokens.map((t) => (
        <text key={t.id} x={t.bbox[0]} y={t.bbox[1] + t.bbox[3] - 1.5} fontSize={t.bbox[3]} fill="var(--ink)"
              fontFamily="IBM Plex Sans, sans-serif">{t.text}</text>
      ))}
      {marks.filter((m) => m.f.status !== "auto_accepted").map(({ f, box }) => (
        <path key={f.name} d={cloudPath(box![0], box![1], box![2], box![3], 5)} fill="rgba(47,49,105,.08)"
              stroke="var(--red)" strokeWidth="1.3" style={{ cursor: onSelect ? "pointer" : "default" }}
              onClick={() => onSelect?.(f.name)}>
          <title>{`${label(f.name)}: ${f.value} — ${f.note || "needs review"}`}</title>
        </path>
      ))}
      {focus && marks.filter((m) => m.f.name === focus).map(({ f, box }) => (
        <path key={`focus-${f.name}`} className="focus-cloud" d={cloudPath(box![0], box![1], box![2], box![3], 7)}
              fill="none" stroke="var(--red)" strokeWidth="2.6" />
      ))}
      {hl && <path d={cloudPath(hl[0], hl[1], hl[2], hl[3], 7)} fill="rgba(47,49,105,.1)" stroke="var(--red)" strokeWidth="2.4" />}
    </>
  );

  // close-up: a second view of the same drawing, framed on the selected value (the sheet text is small at full size)
  const zw = 300, zh = 110;
  const zoom = zoomBox && (
    <svg className="closeup" viewBox={`${zoomBox[0] + zoomBox[2] / 2 - zw / 2} ${zoomBox[1] + zoomBox[3] / 2 - zh / 2} ${zw} ${zh}`}
         role="img" aria-label="Close-up of the selected value">{content}</svg>
  );

  return (
    <figure className="sheet-wrap" style={{ margin: 0 }}>
      <svg viewBox={`-10 -10 ${sheet.width + 20} ${H + 20}`} role="img" aria-label={`Drawing ${sheet.id} with extracted fields marked`}>
        {content}
      </svg>
      {zoom}
      <figcaption className="sheet-legend">
        <span><i className="sw auto" />Accepted automatically</span>
        <span><i className="sw review" />Needs a person</span>
        <span><i className="sw focus" />Selected</span>
        {caption && <span style={{ marginLeft: "auto" }}>{caption}</span>}
      </figcaption>
    </figure>
  );
}
