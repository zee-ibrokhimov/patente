/** The readiness gauge.
 *
 *  A semicircle rather than a bar or a ring, for one reason: the 90% pass threshold has
 *  to sit ON the same scale as the score. "62%" alone floats — 62% of what, and how far
 *  from good? With the bar drawn and the threshold marked, the answer is a glance.
 *
 *  The sweep runs red → amber → green because the number IS a judgement about whether
 *  this person is ready to sit a legally required exam. That is the one place in the app
 *  where a traffic-light reading is the honest one.
 *
 *  Drawn as SVG rather than with conic-gradient so the threshold tick and the value arc
 *  share exactly one coordinate system, and so it scales without a second set of numbers.
 */

const R = 78;
const CX = 100;
const CY = 100;
const SWEEP = Math.PI * R; // semicircle arc length

/** Point on the arc at `fraction` of the sweep, 0 = left (0%), 1 = right (100%). */
function pointAt(fraction: number, radius = R): { x: number; y: number } {
  const angle = Math.PI * (1 - fraction);
  return { x: CX + Math.cos(angle) * radius, y: CY - Math.sin(angle) * radius };
}

export interface GaugeOptions {
  /** 0–1, or null when the server refuses to estimate. */
  value: number | null;
  /** 0–1. Drawn as a tick, because a percentage with nothing to beat is not actionable. */
  threshold: number;
}

export function readinessGauge({ value, threshold }: GaugeOptions): SVGSVGElement {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", "0 0 200 132");
  node.setAttribute("class", "gauge-svg");
  node.setAttribute("aria-hidden", "true");

  const arc = (from: number, to: number) => {
    const a = pointAt(from);
    const b = pointAt(to);
    return `M${a.x.toFixed(2)} ${a.y.toFixed(2)} A${R} ${R} 0 0 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`;
  };

  const tick = pointAt(threshold);
  const tickOut = pointAt(threshold, R + 11);
  const shown = value ?? 0;

  node.innerHTML = `
    <defs>
      <linearGradient id="gaugeSweep" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%"   stop-color="#dc2626"/>
        <stop offset="35%"  stop-color="#f59e0b"/>
        <stop offset="62%"  stop-color="#facc15"/>
        <stop offset="100%" stop-color="#16a34a"/>
      </linearGradient>
    </defs>

    <path d="${arc(0, 1)}" fill="none" stroke="#e9edf2" stroke-width="17" stroke-linecap="round"/>

    ${value === null ? "" : `
    <path d="${arc(0, 1)}" fill="none" stroke="url(#gaugeSweep)" stroke-width="17"
          stroke-linecap="round"
          stroke-dasharray="${(SWEEP * shown).toFixed(2)} ${SWEEP.toFixed(2)}"/>`}

    <path d="M${tick.x.toFixed(2)} ${tick.y.toFixed(2)} L${tickOut.x.toFixed(2)} ${tickOut.y.toFixed(2)}"
          stroke="#0f172a" stroke-width="2.5" stroke-linecap="round"/>

    <text x="10"  y="126" font-size="11" fill="#98a2b3" font-weight="600">0%</text>
    <text x="190" y="126" font-size="11" fill="#98a2b3" font-weight="600" text-anchor="end">100%</text>
    <text x="100" y="12"  font-size="11" fill="#98a2b3" font-weight="600" text-anchor="middle">50%</text>
    <text x="${(tickOut.x + 8).toFixed(1)}" y="${(tickOut.y - 2).toFixed(1)}"
          font-size="11" fill="#16a34a" font-weight="700">${Math.round(threshold * 100)}%</text>
  `;
  return node;
}
