"use client";

import { motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";

const DOT_COUNT = 50; // mirrors the 50-invoice batch
const GRID_COLS = 10;
const GRID_ROWS = 5;

type Phase = "pulse" | "scatter" | "grid";

function gridPositions(): { x: number; y: number }[] {
  const positions: { x: number; y: number }[] = [];
  for (let i = 0; i < DOT_COUNT; i++) {
    const col = i % GRID_COLS;
    const row = Math.floor(i / GRID_COLS);
    // Center a 10x5 grid inside a 480x300 canvas: 24px spacing, 12px dot.
    const x = 240 - (GRID_COLS - 1) * 12 + col * 24;
    const y = 150 - (GRID_ROWS - 1) * 12 + row * 24;
    positions.push({ x, y });
  }
  return positions;
}

function scatterPositions(): { x: number; y: number }[] {
  // Deterministic-ish scatter (seeded by index) so SSR/hydration never flickers.
  const positions: { x: number; y: number }[] = [];
  for (let i = 0; i < DOT_COUNT; i++) {
    const angle = (i / DOT_COUNT) * Math.PI * 2 + 0.618 * i;
    const radius = 40 + ((i * 37) % 130);
    positions.push({
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius * 0.7,
    });
  }
  return positions;
}

/**
 * The "Map-Reduce Fan-Out": one supervisor node pulses, fans out into the
 * 50-invoice batch, then snaps into an ordered mathematical grid.
 * Full sequence stays under ~1.2s with spring physics.
 */
export function FanOutVisual() {
  const [phase, setPhase] = useState<Phase>("pulse");

  const targets = useMemo(() => gridPositions(), []);
  const scatter = useMemo(() => scatterPositions(), []);

  useEffect(() => {
    const t1 = setTimeout(() => setPhase("scatter"), 350);
    const t2 = setTimeout(() => setPhase("grid"), 700);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, []);

  const spring = { type: "spring", stiffness: 260, damping: 22 } as const;

  return (
    <div className="relative h-[300px] w-[480px] max-w-full" aria-hidden>
      {/* Supervisor core */}
      <motion.div
        className="absolute left-1/2 top-1/2 z-10 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary shadow-[0_0_24px_rgba(13,148,251,0.9)]"
        animate={{
          scale: phase === "pulse" ? [1, 1.7, 1] : 0.7,
          opacity: phase === "grid" ? 0.9 : 1,
        }}
        transition={
          phase === "pulse"
            ? { duration: 0.5, repeat: Infinity }
            : spring
        }
      />

      {/* 50 isolated worker dots */}
      {Array.from({ length: DOT_COUNT }).map((_, i) => {
        const scatterPos = scatter[i];
        const gridPos = targets[i];
        const isSuccess = i < 41; // 41/50 settled deterministic path
        return (
          <motion.div
            key={i}
            className="absolute left-1/2 top-1/2 h-2.5 w-2.5 rounded-full"
            style={{
              backgroundColor: phase === "grid" && isSuccess ? "#0E9F6E" : "#0D94FB",
            }}
            initial={{ x: 0, y: 0, scale: 0.2, opacity: 0 }}
            animate={{
              x: phase === "scatter" ? scatterPos.x : gridPos.x,
              y: phase === "scatter" ? scatterPos.y : gridPos.y,
              scale: phase === "pulse" ? 0 : 1,
              opacity: 1,
            }}
            transition={spring}
          />
        );
      })}
    </div>
  );
}
