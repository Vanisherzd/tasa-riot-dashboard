# 鐳洋過境資料儀表板 Design System

## 0. Research Log

- Embedded refs: shortlisted IBM, Sentry, ClickHouse → picked the operational `taste-skill` discipline with IBM Carbon-inspired density because this is an internal scientific data surface, not a marketing site.
- Layout: adopted `page-grid + intrinsic grid + cluster`; the document owns vertical scrolling and each wide data table owns only its local horizontal overflow.
- StyleGallery: skipped because external reference retrieval is unavailable in the workspace sandbox; the local layout contract covers the required spatial model.
- Imagen drafts: skipped because the brief is operational and data-first, with no expressive or reference-fidelity requirement.

## 1. Atmosphere & Identity

A quiet laboratory mission console: precise, trustworthy, and dense without feeling cramped. The signature is the cyan orbital line and cool blue-gray instrument surfaces, used only to guide the eye through live telemetry and not as decoration.

## 2. Color

All colors are CSS custom properties in `style.css`; no component owns a raw color.

| Token | Light | Dark | Role |
|---|---:|---:|---|
| `--canvas` | `#f4f7fb` | `#07111f` | page background |
| `--surface` | `#ffffff` | `#0c1a2b` | primary panels |
| `--surface-raised` | `#eaf1f8` | `#12253a` | secondary bands |
| `--ink` | `#101828` | `#eef6ff` | primary text |
| `--ink-muted` | `#526173` | `#9bb0c8` | secondary text |
| `--line` | `#cbd7e6` | `#243b54` | borders and grid lines |
| `--accent` | `#0062ff` | `#55b8ff` | links, focus, orbital line |
| `--signal` | `#006b4b` | `#43d6a0` | CRC-correct data |
| `--warning` | `#a15c00` | `#ffb454` | degraded or unknown data |

## 3. Typography

- Sans: system UI with CJK fallbacks; Mono: system monospace. No network fonts.
- Display: `clamp(2.5rem, 7vw, 5.25rem)`, weight 720, line-height 1.02; at narrow widths use `clamp(2.5rem, 15vw, 4rem)`.
- H2: `1.375rem`, weight 680, line-height 1.25.
- H3: `1rem`, weight 650, line-height 1.4.
- Body: `1rem`, line-height 1.6. Small: `0.875rem`, line-height 1.5. Caption: `0.75rem`.
- Numeric telemetry uses tabular figures; labels never drop below 12px.

## 4. Spacing & Layout

- Base unit: 4px. Tokens: `--s1` 4px, `--s2` 8px, `--s3` 12px, `--s4` 16px, `--s6` 24px, `--s8` 32px, `--s12` 48px, `--s16` 64px.
- Max content width: 1440px; fluid gutter: `clamp(16px, 4vw, 56px)`.
- The document is the sole vertical scroll owner. `.table-wrap` owns local horizontal overflow and is keyboard focusable.
- Metric regions use overflow-safe intrinsic grids; chart panels stack at full content width and their 1200px SVG viewport scrolls locally when space is narrower.
- At 390px, all primary regions become one column; tables retain semantic columns inside their labeled horizontal viewport.

## 5. Components

### Metric
- Structure: label, large value, optional detail.
- Variants: neutral, signal. States: static; empty values render an em dash.
- Layout: intrinsic grid. Accessibility: label precedes value in source order.

### Panel
- Structure: section header, optional explanatory copy, content.
- Variants: standard, featured. Depth: borders plus tonal shift, no shadows.
- Empty state: inline explanation with the next action (`sh web/update.sh`).

### Data table
- Structure: caption, header row, body rows.
- States: populated and empty. Links expose hover, active, and focus-visible states.
- Layout: a locally scrollable `table-wrap`; header cells use a distinct tonal band.
- Accessibility: semantic table markup, caption, row headers where appropriate, keyboard-focusable overflow wrapper.

### Packet contents
- The network and device `封包內容` tabs show complete packet history, newest reception first. Dashboard previews show the latest ten packets.
- RIOT groups packets by pass in native `details` with the newest two passes open. The summary names the UTC+8 pass time, source file, packet count and CRC-ok count. Within a pass, descending UART line number supplies reception order; the packet's Unix time is a separate decoded field, never a reception timestamp.
- Raw hex is always visible in each packet row, using the system mono font. CRC-8 labels say `通過` or `失敗`; failed packets replace all decoded fields with `不可信，已隱藏`, including the address. Device association for failed packets is only a candidate based on the received address bytes.
- FOSSA shows the original message/payload, transport bytes as text and hex, link/device identity, UTC+8 times, and escaped record metadata. Payloads are hard-decoded: printable text is shown as text, a leading float32 lat/lng/alt triple (Hub Kaitai example layout) is shown as GPS and plotted as small dots, anything else is hex only. Hub GPS-fix status remains a separate feature.
- Payload and metadata blocks wrap long unbroken input at a 52ch measure; tables scroll only inside their labeled, focusable wrapper. Pass summaries wrap, have a visible focus ring, and use spacing tokens for a comfortable touch target.
- Empty states distinguish no RIOT packets from no cached FOSSA messages; a Hub packet counter does not imply locally available packet contents.

### Chart
- Structure: accessible inline SVG title and description, axes, grid, grouped bars, legend.
- Variants: per-pass totals/CRC, SNR-bin totals/CRC, FOSSA daily totals/provisional-valid, and cross-network daily comparison.
- Empty state: text panel rather than an empty SVG.

### Source section
- Structure: source kicker, section title and scope note followed by metrics, tables and charts.
- Variants: FOSSA Nexus and cross-network comparison. The document remains the vertical scroll owner.
- Empty state: names the missing data and shows the webhook endpoint format as the next action.
- FOSSA uses `--warning` for total-message bars and `--signal` for provisional-valid bars; legends and SVG titles carry the meaning without relying on color alone.

### Download link
- Structure: text link with file purpose and filename.
- States: default, hover, active, focus-visible. No external icon dependency.

## 6. Motion & Interaction

No autonomous or decorative motion. Links change color/underline on hover, switch to primary ink on active, and use a 2px focus-visible ring. `prefers-reduced-motion` disables the short color transition.

Packet expansion uses native `details` without JavaScript. Routing uses inline JavaScript. Leaflet 1.9.4 is vendored with its license and embedded at build time, so no script, stylesheet, font or library is fetched from a CDN. Satellite imagery still needs the existing Esri tile service; all packet contents and GPS status remain available offline.

## 7. Depth & Surface

Mixed but restrained: one-pixel borders separate panels and a single tonal shift distinguishes featured content. No box shadows. Radius tokens are `--r-sm` 4px and `--r-md` 8px; panels use 8px, controls and tags use 4px.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA: 4.5:1 body contrast, 3:1 large text and chart marks, visible focus for every link and scroll region, semantic landmarks and tables.
- Both light and dark system themes are supported. SVG charts carry text alternatives and never encode CRC status by color alone because legends and labels name both series.
- Accepted debt: dense tables require local horizontal scrolling on narrow screens; preserving all scientific columns is more useful than hiding them. The wrapper is labeled and keyboard focusable.
- FOSSA packet payloads are hard-decoded (text / float32 GPS / hex); the GPS layout is provisional until the vendor confirms the frame format. GPS-fix status still uses the Hub position report.
