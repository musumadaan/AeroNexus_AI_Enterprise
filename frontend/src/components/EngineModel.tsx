import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useFrame, type ThreeEvent } from "@react-three/fiber";
import { useMemo, useRef, useState } from "react";
import * as THREE from "three";

/** Health score → palette colour (light = healthy, deep indigo = worn). */
const STOPS = ["#2f3169", "#53558c", "#8284ac", "#b5b6d6", "#dedff1"].map((c) => new THREE.Color(c));
export function healthColor(score: number | null | undefined): THREE.Color {
  if (score === null || score === undefined || Number.isNaN(score)) return new THREE.Color("#c9cae3");
  const t = Math.max(0, Math.min(1, score / 100)) * (STOPS.length - 1);
  const i = Math.min(STOPS.length - 2, Math.floor(t));
  return STOPS[i].clone().lerp(STOPS[i + 1], t - i);
}

export interface PartSpec { id: string; label: string; x: number; len: number; r0: number; r1: number; stages: number }
export const PARTS: PartSpec[] = [
  { id: "fan", label: "Fan", x: -2.75, len: 0.32, r0: 1.32, r1: 1.32, stages: 1 },
  { id: "lpc", label: "LPC", x: -1.95, len: 0.75, r0: 0.95, r1: 0.82, stages: 3 },
  { id: "hpc", label: "HPC", x: -0.85, len: 1.35, r0: 0.78, r1: 0.52, stages: 6 },
  { id: "combustor", label: "Combustor", x: 0.3, len: 0.7, r0: 0.62, r1: 0.62, stages: 1 },
  { id: "hpt", label: "HPT", x: 1.0, len: 0.45, r0: 0.58, r1: 0.66, stages: 2 },
  { id: "lpt", label: "LPT", x: 1.75, len: 0.9, r0: 0.7, r1: 0.92, stages: 4 },
  { id: "bleed", label: "Bleed & nozzle", x: 2.65, len: 0.75, r0: 0.86, r1: 0.5, stages: 1 },
];

interface Props {
  scores: Record<string, number | null | undefined>;
  selected?: string | null;
  onSelect?: (id: string) => void;
  exploded?: boolean;
  cutaway?: boolean;
  labels?: boolean;
  spin?: number; // 0..1 fan speed
  alertIds?: string[];
}

function Part({ p, color, selected, hovered, onPointer, offset, alert }: {
  p: PartSpec; color: THREE.Color; selected: boolean; hovered: boolean; offset: number; alert: boolean;
  onPointer: (e: ThreeEvent<PointerEvent | MouseEvent>, kind: "over" | "out" | "click") => void;
}) {
  const group = useRef<THREE.Group>(null);
  const mat = useRef<THREE.MeshStandardMaterial>(null);
  const rotor = useRef<THREE.Group>(null);
  useFrame((state, dt) => {
    if (group.current) group.current.position.x += (p.x + offset - group.current.position.x) * Math.min(1, dt * 5);
    if (mat.current) {
      const pulse = alert ? 0.25 + 0.25 * Math.sin(state.clock.elapsedTime * 4) : 0;
      mat.current.emissiveIntensity = (selected ? 0.55 : hovered ? 0.3 : 0) + pulse;
      mat.current.color.lerp(color, Math.min(1, dt * 4));
    }
    if (rotor.current && p.id !== "combustor") rotor.current.rotation.x += dt * (p.id === "fan" || p.id === "lpc" || p.id === "lpt" ? 2.2 : 4.5) * (group.current?.userData.spin ?? 1);
  });
  const handlers = {
    onPointerOver: (e: ThreeEvent<PointerEvent>) => onPointer(e, "over"),
    onPointerOut: (e: ThreeEvent<PointerEvent>) => onPointer(e, "out"),
    onClick: (e: ThreeEvent<MouseEvent>) => onPointer(e, "click"),
  };
  const emissive = selected ? "#8284ac" : "#b5b6d6";
  return (
    <group ref={group} position={[p.x, 0, 0]}>
      {p.id === "fan" ? (
        <group {...handlers}>
          <group ref={rotor}>
            {Array.from({ length: 22 }).map((_, i) => (
              <mesh key={i} rotation={[(i / 22) * Math.PI * 2, 0, 0]} position={[0, 0, 0]}>
                <boxGeometry args={[0.07, 1.25, 0.2]} />
                <meshStandardMaterial ref={i === 0 ? mat : undefined} color={color} metalness={0.55} roughness={0.35} emissive={emissive} />
              </mesh>
            ))}
          </group>
          <mesh rotation={[0, 0, -Math.PI / 2]} position={[-0.32, 0, 0]}>
            <coneGeometry args={[0.36, 0.55, 32]} />
            <meshStandardMaterial color="#f3f3fa" metalness={0.4} roughness={0.3} />
          </mesh>
          <mesh rotation={[0, 0, Math.PI / 2]}>
            <cylinderGeometry args={[p.r0, p.r0, p.len, 48, 1, true]} />
            <meshStandardMaterial color={color} transparent opacity={0.18} side={THREE.DoubleSide} />
          </mesh>
        </group>
      ) : (
        <group {...handlers}>
          <mesh rotation={[0, 0, Math.PI / 2]}>
            <cylinderGeometry args={[p.r1, p.r0, p.len, 48, 1, p.id === "bleed"]} />
            <meshStandardMaterial ref={mat} color={color} metalness={0.45} roughness={0.38} emissive={emissive}
                                  side={p.id === "bleed" ? THREE.DoubleSide : THREE.FrontSide} />
          </mesh>
          <group ref={rotor}>
            {Array.from({ length: p.stages }).map((_, i) => {
              const xx = -p.len / 2 + ((i + 0.5) * p.len) / p.stages;
              const rr = p.r0 + ((p.r1 - p.r0) * (i + 0.5)) / p.stages;
              return (
                <mesh key={i} position={[xx, 0, 0]} rotation={[0, Math.PI / 2, 0]}>
                  <torusGeometry args={[rr + 0.006, 0.012, 8, 48]} />
                  <meshStandardMaterial color="#53558c" metalness={0.6} roughness={0.3} />
                </mesh>
              );
            })}
          </group>
          {p.id === "combustor" && Array.from({ length: 10 }).map((_, i) => {
            const a = (i / 10) * Math.PI * 2;
            return (
              <mesh key={i} position={[0, Math.cos(a) * 0.66, Math.sin(a) * 0.66]}>
                <sphereGeometry args={[0.055, 12, 12]} />
                <meshStandardMaterial color="#ffffff" emissive="#dedff1" emissiveIntensity={1.2} />
              </mesh>
            );
          })}
          {p.id === "bleed" && [0.55, -0.55].map((z) => (
            <mesh key={z} position={[-1.7, 0.95, z]} rotation={[0, 0, Math.PI / 2]}>
              <cylinderGeometry args={[0.05, 0.05, 2.0, 12]} />
              <meshStandardMaterial color={color} metalness={0.5} roughness={0.35} />
            </mesh>
          ))}
        </group>
      )}
    </group>
  );
}

function Scene({ scores, selected, onSelect, exploded, cutaway, labels = true, spin = 1, alertIds = [] }: Props) {
  const [hover, setHover] = useState<string | null>(null);
  const root = useRef<THREE.Group>(null);
  const colors = useMemo(() => Object.fromEntries(PARTS.map((p) => [p.id, healthColor(scores[p.id])])), [scores]);
  useFrame(() => {
    root.current?.children.forEach((c) => { c.userData.spin = spin; });
  });
  const onPointer = (id: string) => (e: ThreeEvent<PointerEvent | MouseEvent>, kind: "over" | "out" | "click") => {
    e.stopPropagation();
    if (kind === "over") { setHover(id); document.body.style.cursor = "pointer"; }
    if (kind === "out") { setHover((h) => (h === id ? null : h)); document.body.style.cursor = ""; }
    if (kind === "click") onSelect?.(id);
  };
  return (
    <>
      <ambientLight intensity={0.75} />
      <directionalLight position={[4, 6, 5]} intensity={1.6} />
      <directionalLight position={[-5, -2, -4]} intensity={0.6} color="#b5b6d6" />
      <pointLight position={[0, 0, 3]} intensity={0.6} color="#dedff1" />
      <group ref={root} rotation={[0, -0.5, 0]}>
        {PARTS.map((p, i) => (
          <group key={p.id}>
            <Part p={p} color={colors[p.id]} selected={selected === p.id} hovered={hover === p.id} onPointer={onPointer(p.id)}
                  offset={exploded ? (i - 3) * 0.55 : 0} alert={alertIds.includes(p.id)} />
            {labels && (
              <Html position={[p.x + (exploded ? (i - 3) * 0.55 : 0), Math.max(p.r0, p.r1) + 0.28, 0]} center zIndexRange={[5, 0]}>
                <div className={`label3d ${selected === p.id ? "sel" : ""}`}>
                  {p.label}{scores[p.id] !== null && scores[p.id] !== undefined ? ` · ${Math.round(scores[p.id] as number)}` : ""}
                </div>
              </Html>
            )}
          </group>
        ))}
        {/* shaft */}
        <mesh rotation={[0, 0, Math.PI / 2]}>
          <cylinderGeometry args={[0.06, 0.06, 6.2, 16]} />
          <meshStandardMaterial color="#8284ac" metalness={0.7} roughness={0.25} />
        </mesh>
        {/* nacelle / bypass duct */}
        {!cutaway && (
          <mesh rotation={[0, 0, Math.PI / 2]} position={[-1.0, 0, 0]}>
            <cylinderGeometry args={[1.42, 1.48, 4.1, 64, 1, true]} />
            <meshStandardMaterial color="#dedff1" transparent opacity={0.16} side={THREE.DoubleSide} metalness={0.2} roughness={0.6} />
          </mesh>
        )}
      </group>
      <OrbitControls enablePan={false} minDistance={4} maxDistance={12} autoRotate={!selected} autoRotateSpeed={0.5} />
    </>
  );
}

export function EngineCanvas(props: Props & { height?: number }) {
  return (
    <Canvas camera={{ position: [1.5, 2.2, 6.5], fov: 42 }} dpr={[1, 2]} gl={{ antialias: true, preserveDrawingBuffer: true }}
            onPointerMissed={() => props.onSelect?.("")} style={{ height: props.height ?? "100%" }}>
      <Scene {...props} />
    </Canvas>
  );
}
