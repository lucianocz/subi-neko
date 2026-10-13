/** Fraction of the list viewport kept free below the lowest active row. */
export const FOLLOW_BOTTOM_MARGIN = 0.22;
/** Where the active group's top lands after a follow scroll (fraction of viewport), if it fits. */
const FOLLOW_LANDING_INSET = 0.25;

export interface FollowScrollInput {
  /** Positions of the events active right now (any order, may overlap). */
  indices: readonly number[];
  scrollTop: number;
  viewportHeight: number;
  rowHeight: number;
  totalHeight: number;
}

/**
 * New scrollTop for Follow playback, or null when the active rows are already
 * comfortably visible. Pure arithmetic over the fixed row geometry — no DOM
 * reads per row, works for rows the virtualizer has not mounted.
 *
 * The span covers every active row, from the topmost to the lowest. We scroll
 * when the lowest row would intrude into the bottom safety margin, or the span
 * starts above the viewport (backward seek / replay). A scroll then lands the
 * span near the top quarter, so the next scroll is a good while away (no
 * per-event creeping, and landing never re-triggers). A span taller than the
 * viewport minus margin cannot fit: it aligns its top to the viewport top and
 * scrolls again only if that top leaves the viewport.
 */
export function followScrollTarget(input: FollowScrollInput): number | null {
  const { indices, scrollTop, viewportHeight, rowHeight, totalHeight } = input;
  if (indices.length === 0 || viewportHeight <= 0) return null;

  let first = indices[0];
  let last = indices[0];
  for (const i of indices) {
    if (i < first) first = i;
    if (i > last) last = i;
  }
  const spanTop = first * rowHeight;
  const spanBottom = (last + 1) * rowHeight;
  const viewBottom = scrollTop + viewportHeight;
  const maxScroll = Math.max(0, totalHeight - viewportHeight);
  const clamp = (v: number) => Math.min(maxScroll, Math.max(0, Math.round(v)));

  // Margin never squeezes out room for a whole row.
  const margin = Math.min(viewportHeight * FOLLOW_BOTTOM_MARGIN, Math.max(0, viewportHeight - rowHeight));
  const spanHeight = spanBottom - spanTop;

  if (spanHeight + margin > viewportHeight) {
    // Impossible to fit: keep the span's top in view, nothing else.
    const topVisible = spanTop >= scrollTop && spanTop <= viewBottom - rowHeight;
    if (topVisible) return null;
    const target = clamp(spanTop);
    return target === Math.round(scrollTop) ? null : target;
  }

  const fits = spanTop >= scrollTop && spanBottom + margin <= viewBottom;
  // At the very end of the list there is nothing more to scroll to.
  if (fits || (spanTop >= scrollTop && scrollTop >= maxScroll)) return null;

  const inset = Math.min(viewportHeight * FOLLOW_LANDING_INSET, viewportHeight - margin - spanHeight);
  const target = clamp(spanTop - Math.max(0, inset));
  return target === Math.round(scrollTop) ? null : target;
}
