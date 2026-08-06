# UI_DIRECTION.md — the Backline design language

**Subject**: a label back-office run like a studio console — the money desk of an
independent label at 1 a.m. Dark, dense, calm, precise. This is a **data instrument**,
not a marketing page. Every UI decision in `ui/` follows this document; deviations get
a DECISIONS entry.

The one-line test for any new surface: *would this look at home racked between a
compressor and a patchbay?* If it's shouting, it's wrong.

---

## 1. Palette

Graphite base, one warm accent, two signal colors. Nothing else carries meaning.

| Token | Value | Use — and *only* this use |
|---|---|---|
| `--bg` | `#0B0C0E` | app background (near-black graphite) |
| `--panel` | `#121316` | panels, cards, rails |
| `--panel-2` | `#17181C` | raised surfaces: table headers, drawers, popovers |
| `--edge` | `#26282E` | hairline borders, dividers |
| `--edge-strong` | `#34373F` | focused/hovered borders |
| `--text` | `#E6E7E9` | primary text |
| `--text-dim` | `#9A9DA3` | secondary text, labels, captions |
| `--text-faint` | `#5C5F66` | tertiary: placeholders, disabled, empty states |
| `--amber` | `#F2A33C` | **live/active only**: running spans, pending approvals, streaming state |
| `--amber-dim` | `#F2A33C29` | amber at 16% — live-row washes, pulse halos |
| `--green` | `#3ECF8E` | **money-in / approved only** |
| `--red` | `#E5484D` | **flags / rejections / errors only** |
| `--blue` | `#6E9EE8` | hyperlink/citation affordance (quiet, used sparingly) |

Rules:

- **Amber is the "tape is rolling" light.** It means *something is happening right
  now* (an active span, a batch waiting on a human). It never decorates. If nothing
  is live, the screen has no amber on it.
- Green is reserved for approved money states and positive deltas. Red is reserved
  for flags, rejections, and failures. Neither is ever used "for contrast."
- Severity ramp for flags: `error`/`high` red, `warn`/`medium` amber, `info`/`low`
  gray. Guardrail incidents in traces are always red.
- Backgrounds never lighten past `--panel-2`; light mode does not exist.

## 2. Type

Two families, no font soup:

- **Inter Tight** — all UI text, headings, labels. Weights 400/500/600 only.
- **IBM Plex Mono** — **every monetary figure and identifier**: amounts, ISRC, UPC,
  batch ids, run ids, clause numbers, token counts, latencies, SQL. Always with
  `font-variant-numeric: tabular-nums` so columns of money align to the digit.

Money in mono is the signature typographic move: if a number could appear on a royalty
statement, it renders in Plex Mono. Prose about numbers stays in Inter Tight.

Scale (rem): `11px` captions/labels (uppercase, +0.06em tracking, `--text-dim`),
`13px` body/table default, `15px` section heads, `20px` page titles. Density beats
hierarchy-by-size: prefer weight and color over big type.

Both fonts ship as self-hosted `woff2` in `ui/public/fonts/` (no external font
requests; the reviewer's cold clone renders identically offline).

## 3. Space, density, structure

- Base unit 4px. Tables use 8px vertical padding per row; panels pad 16px.
- **Tables are the primary surface.** Row hover = `--panel-2` wash + revealed
  actions. Numeric columns right-align. Header row is `11px` uppercase `--text-dim`.
- Left app rail (56px, icons) → surface-specific left pane (sessions / runs list,
  ~280px) → main content. The trace timeline rail is the exception that earns width.
- Hairline borders (`1px --edge`) over shadows. Radius: 6px panels, 4px controls.
  No blur, no glassmorphism, no gradients except the amber pulse halo.

## 4. The signature element: the live trace timeline

The single most memorable thing in the app — a left-rail vertical span tree that
fills in real time as an agent runs. One aesthetic risk, spent here.

- Structure: `run → iteration → llm_call / tool_call / guardrail / compression`,
  indented by depth, connected by 1px `--edge` rails.
- The **active span pulses amber**: a 2px left edge + `--amber-dim` halo animating
  at ~1.2s ease-in-out (respects `prefers-reduced-motion`: pulse becomes a static
  amber edge).
- Completed spans settle to graphite; their duration and cost print right-aligned
  in Plex Mono (`142ms`, `$0.0031`). Guardrail spans get a red edge and stay red.
- The run header keeps a **cost meter ticking upward in mono** as spans land —
  the "this is costing money right now" readout.
- Span click → detail drawer: attrs table, tool args/results as a JSON tree,
  token counts. Nothing is more than one click from the raw data.

## 5. Motion

Motion is information, never garnish:

- Span arrival: 120ms fade+2px rise. Cost meter: number transitions stepped, no
  tween (money doesn't interpolate).
- Streaming answer text: no fake typewriter — content appears as it arrives.
- Drawers slide 160ms ease-out; hover reveals are instant.
- `prefers-reduced-motion`: all of the above become opacity-only or static.

## 6. Keyboard

The Review Queue is keyboard-first: `j/k` navigate rows, `a` approve, `r` reject
(opens the note field — reject **requires** a note), `Esc` closes. Chat: `Enter`
sends, `Shift+Enter` newline. Focus states are always visible: 1px `--amber`
outline offset 2px (focus is a live state; amber is correct here).

## 7. Quality floor (every surface, no exceptions)

- Responsive to laptop widths (min 1100px content design; nothing breaks at 1280px).
- Visible focus states throughout; semantic HTML; tables are `<table>`.
- Skeleton states for every async panel (graphite shimmer bars, no spinners).
- Empty states tell the user what to do next ("No runs yet — ask something in
  Chat"), in `--text-faint`, never blank panels.
- Abstentions render as a distinct quiet state (bordered `--text-dim` card,
  "abstained" label) — an abstention is correct behavior, not an error.
- All timestamps: `2026-08-06 14:32` UTC, in mono. Relative time only as a
  secondary hint ("4m ago", `--text-faint`).

## 8. Voice

Interface copy is terse and factual: "routed to Counsel · 0.92", "3 flags · 1 high",
"$0.0417 · 6 iterations". No exclamation marks, no "smart" microcopy, no emoji.
The app never congratulates anyone.
