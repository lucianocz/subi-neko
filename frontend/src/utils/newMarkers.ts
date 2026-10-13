/** NEW / UPDATED review markers: the server owns `is_new`; the UI only reads it. */
export interface NewMarkable {
  is_new: boolean;
}

/** Number of flagged entries across the whole list (independent of any active filter). */
export function countNew(items: readonly NewMarkable[]): number {
  return items.reduce((n, item) => (item.is_new ? n + 1 : n), 0);
}

/** `onlyNew` keeps flagged entries; order is preserved and the input is never mutated. */
export function filterNew<T extends NewMarkable>(items: readonly T[], onlyNew: boolean): T[] {
  return onlyNew ? items.filter((item) => item.is_new) : [...items];
}

export const NEW_MARKER_LABEL = 'New or updated since last translation';
