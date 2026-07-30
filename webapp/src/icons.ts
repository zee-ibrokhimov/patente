/** Inline SVG.
 *
 *  No icon font and no sprite sheet: every glyph here is used once or twice, and a
 *  webfont for nine shapes would be another network request inside Telegram's webview
 *  for less payload than the shapes themselves.
 *
 *  `currentColor` throughout, so an icon takes the colour of whatever it sits in and the
 *  tab bar's active state needs no second icon set.
 */

function svg(paths: string, size = 24, filled = false): SVGSVGElement {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("width", String(size));
  node.setAttribute("height", String(size));
  node.setAttribute("fill", filled ? "currentColor" : "none");
  if (!filled) {
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", "2");
    node.setAttribute("stroke-linecap", "round");
    node.setAttribute("stroke-linejoin", "round");
  }
  node.setAttribute("aria-hidden", "true");
  node.innerHTML = paths;
  return node;
}

export const icons = {
  /** The brand mark: a steering wheel. */
  wheel: (size = 26) =>
    svg(
      `<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/>
       <path d="M12 3v6M4.2 8.5l5.2 2.2M19.8 8.5l-5.2 2.2M6.5 19.5 10.5 14M17.5 19.5 13.5 14"/>`,
      size,
    ),

  clipboard: (size = 26) =>
    svg(
      `<rect x="6" y="4" width="12" height="17" rx="2.5"/>
       <path d="M9.5 4a2.5 2.5 0 0 1 5 0"/>
       <path d="M9.5 10.5h5M9.5 14h5M9.5 17.5h3"/>`,
      size,
    ),

  cap: (size = 26) =>
    svg(`<path d="M2.5 9 12 4.5 21.5 9 12 13.5 2.5 9Z"/><path d="M6.5 11v5c0 1.4 2.5 2.8 5.5 2.8s5.5-1.4 5.5-2.8v-5"/>`, size),

  alert: (size = 16) =>
    svg(`<circle cx="12" cy="12" r="9.5"/><path d="M12 7.5v5M12 16.2v.3"/>`, size),

  check: (size = 16) =>
    svg(`<circle cx="12" cy="12" r="9.5"/><path d="m8 12.2 2.7 2.7L16 9.6"/>`, size),

  home: (size = 24) =>
    svg(`<path d="M3.5 10.5 12 4l8.5 6.5V19a1.5 1.5 0 0 1-1.5 1.5h-3.5V15h-7v5.5H5A1.5 1.5 0 0 1 3.5 19v-8.5Z"/>`, size, true),

  person: (size = 24) =>
    svg(`<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20.2c.8-3.6 3.8-5.7 7.5-5.7s6.7 2.1 7.5 5.7Z"/>`, size, true),

  chart: (size = 24) =>
    svg(`<rect x="4" y="13" width="3.6" height="7.5" rx="1.2"/><rect x="10.2" y="8.5" width="3.6" height="12" rx="1.2"/><rect x="16.4" y="4" width="3.6" height="16.5" rx="1.2"/>`, size, true),

  gear: (size = 24) =>
    svg(
      `<path d="M12 8.6A3.4 3.4 0 1 0 12 15.4 3.4 3.4 0 0 0 12 8.6Zm8.2 3.4c0-.5 0-1-.1-1.5l2-1.5-2-3.4-2.3 1a8.2 8.2 0 0 0-2.6-1.5L14.8 2h-4l-.4 2.6a8.2 8.2 0 0 0-2.6 1.5l-2.3-1-2 3.4 2 1.5a8.6 8.6 0 0 0 0 3l-2 1.5 2 3.4 2.3-1a8.2 8.2 0 0 0 2.6 1.5l.4 2.6h4l.4-2.6a8.2 8.2 0 0 0 2.6-1.5l2.3 1 2-3.4-2-1.5c.1-.5.1-1 .1-1.5Z"/>`,
      size,
      true,
    ),

  crown: (size = 26) =>
    svg(`<path d="M3 8.5 7 12l5-6.5 5 6.5 4-3.5-1.6 9.5H4.6L3 8.5Z"/><path d="M4.6 19.5h14.8"/>`, size, true),

  star: (size = 26) =>
    svg(`<path d="m12 3.6 2.6 5.5 6 .8-4.4 4.2 1.1 6-5.3-2.9-5.3 2.9 1.1-6L3.4 9.9l6-.8L12 3.6Z"/>`, size, true),

  chevron: (size = 22) => svg(`<path d="m9 5 7 7-7 7"/>`, size),

  flag: (size = 20) => svg(`<path d="M6 20V4M6 5h11l-2.5 4L17 13H6"/>`, size),

  clock: (size = 44) =>
    svg(`<circle cx="12" cy="12" r="9"/><path d="M12 6.5V12l4 2.2"/>`, size),

  cross: (size = 44) => svg(`<path d="m7 7 10 10M17 7 7 17"/>`, size),

  tick: (size = 44) => svg(`<path d="m5 12.5 4.5 4.5L19 7.5"/>`, size),

  info: (size = 24) => svg(`<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.3"/>`, size),

  bulb: (size = 24) =>
    svg(`<path d="M9 17.5h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2h5c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z"/>`, size),

  refresh: (size = 22) =>
    svg(`<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v5h-5"/>`, size),

  target: (size = 26) =>
    svg(`<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>`, size),
};

/** Decorative artwork for the two mode cards.
 *
 *  Drawn rather than shipped as raster images: they must tint with the card, scale on any
 *  screen, and cost nothing to download. They are DECORATION — every card also carries an
 *  icon, a title, a description and a chip, because artwork is the first thing clipped on
 *  a narrow phone and must never be what tells you which mode you are looking at.
 */
export const art = {
  /** A stopwatch over an answer sheet: timed, and graded at the end. */
  exam: (): SVGSVGElement => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 200 200");
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = `
      <g opacity=".55">
        <rect x="58" y="18" width="104" height="128" rx="10" fill="#fff" stroke="#f0c9c9" stroke-width="2" transform="rotate(6 110 82)"/>
        ${Array.from({ length: 4 }, (_, r) =>
          Array.from({ length: 4 }, (_, c) =>
            `<rect x="${74 + c * 21}" y="${44 + r * 24}" width="14" height="14" rx="4"
                   fill="${r === 1 && c === 1 ? "#f3b0b0" : "none"}" stroke="#f0c9c9" stroke-width="2"
                   transform="rotate(6 110 82)"/>`).join("")).join("")}
      </g>
      <circle cx="112" cy="128" r="52" fill="#fff" stroke="#d92d20" stroke-width="7"/>
      <path d="M112 128 V88 A40 40 0 0 1 148 132 Z" fill="#f19c98"/>
      <rect x="104" y="64" width="16" height="12" rx="3" fill="#d92d20"/>
      <rect x="99" y="58" width="26" height="9" rx="4" fill="#d92d20"/>
      <path d="M112 128 L138 118" stroke="#d92d20" stroke-width="6" stroke-linecap="round"/>
      <circle cx="112" cy="128" r="6" fill="#d92d20"/>
      ${Array.from({ length: 12 }, (_, i) => {
        const a = (i * 30 * Math.PI) / 180;
        const x1 = 112 + Math.sin(a) * 42, y1 = 128 - Math.cos(a) * 42;
        const x2 = 112 + Math.sin(a) * 47, y2 = 128 - Math.cos(a) * 47;
        return `<path d="M${x1} ${y1} L${x2} ${y2}" stroke="#c9a5a5" stroke-width="3" stroke-linecap="round"/>`;
      }).join("")}`;
    return node;
  },

  /** A mandatory-direction sign over a calm road: guidance, not judgement. */
  practice: (): SVGSVGElement => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 200 200");
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = `
      <g opacity=".5">
        <path d="M0 176 Q60 150 100 176 T200 168 V200 H0 Z" fill="#cfebd8"/>
        <circle cx="34" cy="126" r="15" fill="#cfebd8"/><rect x="31" y="136" width="6" height="16" fill="#bfe0cb"/>
        <circle cx="172" cy="118" r="12" fill="#cfebd8"/><rect x="169" y="126" width="5" height="14" fill="#bfe0cb"/>
      </g>
      <rect x="106" y="96" width="7" height="80" rx="3" fill="#9fb4a7"/>
      <circle cx="110" cy="66" r="42" fill="#0b5fa5" stroke="#fff" stroke-width="6"/>
      <path d="M110 88 V50 M110 46 l-13 14 M110 46 l13 14" stroke="#fff" stroke-width="9" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      <g>
        <rect x="26" y="140" width="118" height="46" rx="11" fill="#fff" stroke="#d8eede" stroke-width="2"/>
        <rect x="40" y="152" width="58" height="6" rx="3" fill="#d3dcd6"/>
        <rect x="40" y="166" width="40" height="6" rx="3" fill="#cfeadb"/>
        <circle cx="122" cy="163" r="15" fill="#22a35c"/>
        <path d="m115 163 5 5 9-10" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </g>`;
    return node;
  },
};
