import { rest } from "../api";

export interface Frame {
  cycle: number; rul: number; low: number; high: number; health: number; true_rul: number; anomaly: boolean;
  components: Record<string, number | null>; raw: Record<string, number>; z: Record<string, number>; i?: number;
}
export interface TwinEvent { cycle: number; kind: string; severity: string; sensor: string | null; value: number; message: string }
export interface Telemetry {
  available: boolean; message?: string; subset: string; unit: number; sensors: string[]; key_sensors: string[]; frames: Frame[];
  events: TwinEvent[]; health_threshold: number; interval_halfwidth: number; simulated: boolean; injected: Injection[];
  model: { selected: string; test_rmse: number; coverage: number }; notes: string[];
}
export interface Injection { type: "spike" | "drift" | "stuck" | "noise"; sensor: string; start: number; magnitude: number }
export interface Component { id: string; label: string; sensors: string[]; what: string }
export interface SensorInfo { name: string; label: string; unit: string }
export interface TwinMeta { components: Component[]; sensors: Record<string, SensorInfo>; engines: Record<string, number[]>; available: boolean; message?: string }

const cache = new Map<string, Promise<Telemetry>>();
export function getTelemetry(subset: string, unit: number, inject: Injection[] = []): Promise<Telemetry> {
  const k = JSON.stringify([subset, unit, inject]);
  if (!cache.has(k)) {
    const p = rest<Telemetry>("/api/twin/telemetry", { body: { subset, unit, inject } });
    p.catch(() => cache.delete(k));
    cache.set(k, p);
  }
  return cache.get(k)!;
}

let metaP: Promise<TwinMeta> | null = null;
export function getMeta(): Promise<TwinMeta> {
  if (!metaP) { metaP = rest<TwinMeta>("/api/twin/meta"); metaP.catch(() => { metaP = null; }); }
  return metaP;
}

export const statusOf = (s: number | null | undefined) =>
  s === null || s === undefined ? "n/a" : s >= 75 ? "good" : s >= 50 ? "watch" : "poor";
