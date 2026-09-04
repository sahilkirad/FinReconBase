import type { Config } from "tailwindcss";

/**
 * Razorpay "Blade"-inspired tokens (locked in the Track 4 brief):
 *   primary action  -> Dodger Blue #0D94FB
 *   nav / deep hdr  -> Prussian Blue #012652
 *   canvas          -> clean white
 *   hairline border -> #EBECF0
 *   semantic pills  -> success green / pending amber / danger red
 * Numbers/IDs always render in font-mono + tabular-nums.
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
    "./store/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: "#0D94FB",
          50: "#E6F5FF",
          600: "#0D94FB",
          700: "#0B7AD0",
        },
        navy: {
          DEFAULT: "#012652",
          800: "#01346B",
          900: "#012652",
        },
        canvas: "#FFFFFF",
        line: "#EBECF0",
        success: {
          DEFAULT: "#0E9F6E",
          soft: "rgba(14, 159, 110, 0.10)",
        },
        pending: {
          DEFAULT: "#D97706",
          soft: "rgba(217, 119, 6, 0.10)",
        },
        danger: {
          DEFAULT: "#DC2626",
          soft: "rgba(220, 38, 38, 0.10)",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      borderRadius: {
        DEFAULT: "6px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(1, 38, 82, 0.06), 0 4px 16px rgba(1, 38, 82, 0.06)",
      },
    },
  },
  plugins: [],
};

export default config;
