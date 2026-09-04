"use client";

import { motion } from "framer-motion";
import { useMemo } from "react";

const NODE_COUNT = 50; // mirrors the 50-invoice batch

interface ScatterPoint {
  x: number; // 0..100 (% of canvas width)
  y: number; // 0..100 (% of canvas height)
  delay: number; // when the sweep line reaches this node
}

/**
 * Deterministic scatter (no Math.random) so SSR/hydration never flickers.
 * Nodes sit in two loose bands above/below the center "ledger" line; the
 * sweep delay maps left->right so the line appears to capture them in order.
 */
function scatterPoints(): ScatterPoint[] {
  const points: ScatterPoint[] = [];
  let seed = 42;
  const rand = () => {
    // LCG — deterministic per render.
    seed = (seed * 9301 + 49297) % 233280;
    return seed / 233280;
  };
  for (let i = 0; i < NODE_COUNT; i++) {
    const upper = i % 2 === 0;
    const x = 4 + rand() * 92; // 4..96%
    const band = 10 + rand() * 26; // 10..36%
    const y = upper ? band : 64 + (rand() * 26); // 10..36% or 64..90%
    // Line grows left->right over ~0.85s starting at ~0.12s.
    const delay = 0.12 + (x / 100) * 0.85;
    points.push({ x, y, delay });
  }
  return points;
}

/**
 * "Reconciliation Line" (Razorpay Ray inspiration): a glowing Dodger-blue
 * sweep draws left-to-right across a Prussian-blue canvas. Scattered grey
 * nodes snap onto the line, turn green, and align into a single ordered
 * ledger row — order out of chaos. One-shot, under ~1.2s, aria-hidden.
 */
export function FanOutVisual() {
  const nodes = useMemo(() => scatterPoints(), []);

  return (
    <div className="relative h-full w-full overflow-hidden" aria-hidden>
      {/* Sweeping ledger line: grows left -> right with a soft blue glow */}
      <motion.div
        className="absolute left-0 right-0 top-1/2 h-[2px] origin-left"
        style={{
          top: "calc(50% - 1px)",
          backgroundColor: "#0D94FB",
          boxShadow:
            "0 0 18px 2px rgba(13,148,251,0.65), 0 0 60px 8px rgba(13,148,251,0.25)",
        }}
        initial={{ scaleX: 0 }}
        animate={{ scaleX: 1 }}
        transition={{ duration: 0.85, ease: "easeInOut", delay: 0.12 }}
      />

      {/* Faint scattered nodes — captured (green + aligned) as the sweep passes */}
      {nodes.map((node, i) => (
        <motion.div
          key={i}
          className="absolute h-2 w-2 rounded-full"
          style={{
            left: `${node.x}%`,
            top: `${node.y}%`,
            marginLeft: -4,
            marginTop: -4,
          }}
          initial={{ backgroundColor: "#94a3b8", opacity: 0.28, scale: 0.7 }}
          animate={{
            top: "50%",
            backgroundColor: "#0E9F6E",
            opacity: 1,
            scale: 1.15,
          }}
          transition={{
            delay: node.delay,
            type: "spring",
            stiffness: 300,
            damping: 24,
          }}
        />
      ))}

      {/* Leading sweep head: a brighter core riding the growing line */}
      <motion.div
        className="absolute top-1/2 h-1 w-16 -translate-y-1/2 rounded-full"
        style={{
          backgroundColor: "#7FD1FF",
          boxShadow: "0 0 24px 6px rgba(13,148,251,0.8)",
        }}
        initial={{ left: "-6%" }}
        animate={{ left: "104%" }}
        transition={{ duration: 0.9, ease: "easeInOut", delay: 0.1 }}
      />
    </div>
  );
}
