export interface KeyTargetLike {
  tagName?: string;
  isContentEditable?: boolean;
  getAttribute?(name: string): string | null;
}

const TEXT_ENTRY_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
// Elements that give Space a native meaning of their own (activate / play-pause).
const SPACE_NATIVE_TAGS = new Set(['BUTTON', 'A', 'SUMMARY', 'VIDEO']);

/**
 * Whether a global Space shortcut must stay out of the way: text entry
 * (input/textarea/select/contenteditable/textbox role) or an element that
 * already handles Space itself (buttons — and the native video controls, which
 * would otherwise toggle twice).
 */
export function spaceBelongsToTarget(target: KeyTargetLike | null | undefined): boolean {
  if (!target) return false;
  const tag = target.tagName?.toUpperCase();
  if (tag && (TEXT_ENTRY_TAGS.has(tag) || SPACE_NATIVE_TAGS.has(tag))) return true;
  if (target.isContentEditable) return true;
  const ce = target.getAttribute?.('contenteditable');
  if (ce !== null && ce !== undefined && ce !== 'false') return true;
  const role = target.getAttribute?.('role');
  return role === 'textbox' || role === 'combobox' || role === 'button' || role === 'switch'
    || role === 'checkbox' || role === 'tab' || role === 'menuitem';
}
