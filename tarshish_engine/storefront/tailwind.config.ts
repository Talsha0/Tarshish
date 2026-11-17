import type { Config } from "tailwindcss";

// FindHub design tokens.
//
// One idea carries the look: the yellow shelf label. Everything a shopper should act on
// (the live price) sits on `tag`; everything that vouches for it is `spruce`; the rest
// stays quiet on cool paper. `flash` is reserved for time-limited deals.
//
// The values are RGB triplets in src/app/globals.css, light by default and dark from the
// system setting or <html data-theme="dark">. `on-*` is the text colour for a filled
// `bg-*` of the same name; `hero` is the banner, which stays deep green in both themes.
const rgb = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  // `dark:` follows the system unless <html data-theme> forces a theme (same rule as the CSS tokens).
  darkMode: [
    "variant",
    ["@media (prefers-color-scheme: dark) { &:not([data-theme=light] *) }", "&:is([data-theme=dark] *)"],
  ],
  theme: {
    extend: {
      colors: {
        paper: rgb("paper"),
        surface: rgb("surface"),
        ink: { DEFAULT: rgb("ink"), soft: rgb("ink-soft"), faint: rgb("ink-faint") },
        line: { DEFAULT: rgb("line"), strong: rgb("line-strong") },
        spruce: { DEFAULT: rgb("spruce"), deep: rgb("spruce-deep"), bright: rgb("spruce-bright"), tint: rgb("spruce-tint") },
        tag: { DEFAULT: rgb("tag"), deep: rgb("tag-deep"), ink: rgb("tag-ink") },
        flash: { DEFAULT: rgb("flash"), tint: rgb("flash-tint") },
        on: { spruce: rgb("on-spruce"), flash: rgb("on-flash") },
        hero: rgb("hero"),
        scrim: rgb("scrim"),
        // Admin portal: light/dark tokens defined as RGB triplets in src/app/admin/admin.css.
        adm: {
          bg: "rgb(var(--adm-bg) / <alpha-value>)",
          panel: "rgb(var(--adm-panel) / <alpha-value>)",
          raised: "rgb(var(--adm-raised) / <alpha-value>)",
          line: "rgb(var(--adm-line) / <alpha-value>)",
          ink: "rgb(var(--adm-ink) / <alpha-value>)",
          soft: "rgb(var(--adm-soft) / <alpha-value>)",
          faint: "rgb(var(--adm-faint) / <alpha-value>)",
          accent: "rgb(var(--adm-accent) / <alpha-value>)",
          "accent-ink": "rgb(var(--adm-accent-ink) / <alpha-value>)",
          ok: "rgb(var(--adm-ok) / <alpha-value>)",
          warn: "rgb(var(--adm-warn) / <alpha-value>)",
          bad: "rgb(var(--adm-bad) / <alpha-value>)",
          console: "rgb(var(--adm-console) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "Assistant", "system-ui", "Segoe UI", "Arial", "sans-serif"],
        display: ["var(--font-display)", "Frank Ruhl Libre", "Georgia", "Times New Roman", "serif"],
      },
      fontSize: {
        // A classic scale (12 14 16 18 21 24 36 48 60) with line heights set per step.
        xs: ["0.75rem", { lineHeight: "1.1rem" }],
        sm: ["0.875rem", { lineHeight: "1.3rem" }],
        base: ["1rem", { lineHeight: "1.5rem" }],
        lg: ["1.125rem", { lineHeight: "1.65rem" }],
        xl: ["1.3125rem", { lineHeight: "1.8rem" }],
        "2xl": ["1.5rem", { lineHeight: "1.9rem" }],
        "3xl": ["2.25rem", { lineHeight: "2.5rem" }],
        "4xl": ["3rem", { lineHeight: "3.15rem" }],
        "5xl": ["3.75rem", { lineHeight: "3.85rem" }],
      },
      maxWidth: { shell: "82rem" },
      boxShadow: {
        card: "0 1px 0 rgba(20, 36, 31, 0.04)",
        lift: "0 18px 34px -18px rgba(15, 76, 58, 0.38), 0 2px 6px rgba(20, 36, 31, 0.06)",
        panel: "0 30px 80px -24px rgba(10, 55, 41, 0.45)",
        tag: "0 1px 0 #EFB900",
      },
      keyframes: {
        shimmer: { "100%": { transform: "translateX(100%)" } },
        "flash-pulse": {
          "0%": { boxShadow: "0 0 0 0 rgba(211, 36, 59, 0.55)" },
          "70%, 100%": { boxShadow: "0 0 0 7px rgba(211, 36, 59, 0)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.5s ease-in-out infinite",
        "flash-pulse": "flash-pulse 1.8s ease-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
