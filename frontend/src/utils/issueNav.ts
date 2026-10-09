// Pure helpers behind the Subtitle Editor's Previous / Next issue buttons.
// The index is sorted by `line_index` (the table order), so every lookup is a
// binary search — nothing scans the loaded rows.

export interface IssueTarget {
  event_id: number;
  line_index: number;
  /** 0-based rank in the listing the editor paginates under its filters. */
  position: number;
}

/** First index whose line_index is >= `lineIndex`. */
function lowerBound(items: readonly IssueTarget[], lineIndex: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (items[mid].line_index < lineIndex) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/** First target strictly after `anchor`; with no anchor, the first target. */
export function findNextIssue(items: readonly IssueTarget[], anchor: number | null): IssueTarget | null {
  if (anchor === null) return items[0] ?? null;
  // Strictly after: skip the anchor's own entry if it is one.
  let i = lowerBound(items, anchor);
  if (items[i]?.line_index === anchor) i += 1;
  return items[i] ?? null;
}

/** Last target strictly before `anchor`; with no anchor there is none. */
export function findPrevIssue(items: readonly IssueTarget[], anchor: number | null): IssueTarget | null {
  if (anchor === null) return null;
  return items[lowerBound(items, anchor) - 1] ?? null;
}

export function pageForPosition(position: number, pageSize: number): number {
  return Math.floor(position / pageSize) + 1;
}

/** Drop an event from the index once it has no unresolved issue left. */
export function withoutIssueEvent(items: readonly IssueTarget[], eventId: number): IssueTarget[] {
  return items.some((item) => item.event_id === eventId)
    ? items.filter((item) => item.event_id !== eventId)
    : (items as IssueTarget[]);
}

/**
 * scrollTop that centres a row in the part of the viewport below a sticky
 * header. The browser clamps out-of-range values, so no bounds handling here.
 */
export function centeredScrollTop(opts: {
  scrollTop: number;
  viewportTop: number;
  viewportHeight: number;
  headerHeight: number;
  rowTop: number;
  rowHeight: number;
}): number {
  const rowOffset = opts.rowTop - opts.viewportTop + opts.scrollTop;
  const free = opts.viewportHeight - opts.headerHeight - opts.rowHeight;
  return Math.max(0, rowOffset - opts.headerHeight - free / 2);
}
