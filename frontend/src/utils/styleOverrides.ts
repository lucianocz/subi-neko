/**
 * Pure model behind the Styles & Fonts editor: override drafts, effective values,
 * the shared Source/Replacement column layout and the live-preview CSS.
 *
 * Override semantics (mirrors the backend's `effective_style`): `null` = inherit the
 * source value; `false` and `0` are real overrides.
 */
import { DEFAULT_ASS_COLOUR, assToCssRgba, describeAssColour } from './assColour.ts';

/** Structural subset of `ProjectStyle` (kept local so this module stays dependency-free). */
export interface StyleSource {
  font_name: string;
  font_size: number;
  bold: boolean;
  italic: boolean;
  outline: number;
  shadow: number;
  primary_colour: string | null;
  outline_colour: string | null;
  back_colour: string | null;
  replacement_font_name: string | null;
  replacement_font_size: number | null;
  replacement_bold: boolean | null;
  replacement_italic: boolean | null;
  replacement_outline: number | null;
  replacement_shadow: number | null;
  replacement_primary_colour: string | null;
  replacement_outline_colour: string | null;
  replacement_back_colour: string | null;
}

export interface StyleUpdate {
  replacement_font_name: string | null;
  replacement_font_size: number | null;
  replacement_bold: boolean | null;
  replacement_italic: boolean | null;
  replacement_outline: number | null;
  replacement_shadow: number | null;
  replacement_primary_colour: string | null;
  replacement_outline_colour: string | null;
  replacement_back_colour: string | null;
}

export type TriState = boolean | null;

/** Number inputs hand back numbers, or strings while a decimal is half-typed ("1.", "0.50"). */
export type NumberDraft = number | string;

export interface StyleDraft {
  fontName: string;
  fontSize: NumberDraft;
  bold: TriState;
  italic: TriState;
  outline: NumberDraft;
  shadow: NumberDraft;
  primary: string | null;
  outlineColour: string | null;
  backColour: string | null;
}

export type DraftKey = keyof StyleDraft;

export const SIZE_RANGE = { min: 1, max: 1000 } as const;
export const WIDTH_RANGE = { min: 0, max: 1000 } as const;

/** inherit → on → off → inherit. */
export function cycleTriState(value: TriState): TriState {
  if (value === null) return true;
  return value ? false : null;
}

/** Up to 4 decimals, without float noise (0.1+0.2 → "0.3") or trailing zeros. */
export function formatDecimal(value: number): string {
  return String(Number(value.toFixed(4)));
}

/** '' / non-numeric → null (inherit). Never rounds. */
export function parseDecimalInput(value: NumberDraft | null | undefined): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (value == null || value.trim() === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function draftFromStyle(style: StyleSource): StyleDraft {
  return {
    fontName: style.replacement_font_name ?? '',
    fontSize: style.replacement_font_size ?? '',
    bold: style.replacement_bold,
    italic: style.replacement_italic,
    outline: style.replacement_outline ?? '',
    shadow: style.replacement_shadow ?? '',
    primary: style.replacement_primary_colour,
    outlineColour: style.replacement_outline_colour,
    backColour: style.replacement_back_colour,
  };
}

export function draftToUpdate(draft: StyleDraft): StyleUpdate {
  return {
    replacement_font_name: draft.fontName.trim() || null,
    replacement_font_size: parseDecimalInput(draft.fontSize),
    replacement_bold: draft.bold,
    replacement_italic: draft.italic,
    replacement_outline: parseDecimalInput(draft.outline),
    replacement_shadow: parseDecimalInput(draft.shadow),
    replacement_primary_colour: draft.primary,
    replacement_outline_colour: draft.outlineColour,
    replacement_back_colour: draft.backColour,
  };
}

export function styleToUpdate(style: StyleSource): StyleUpdate {
  return draftToUpdate(draftFromStyle(style));
}

export function isDraftDirty(draft: StyleDraft, style: StyleSource): boolean {
  const a = draftToUpdate(draft);
  const b = styleToUpdate(style);
  return (Object.keys(a) as (keyof StyleUpdate)[]).some((k) => a[k] !== b[k]);
}

const within = (n: number | null, min: number, max: number) => n === null || (n >= min && n <= max);

/** Save is blocked while a typed number is out of the range the API accepts. */
export function isDraftValid(draft: StyleDraft): boolean {
  const size = parseDecimalInput(draft.fontSize);
  return (
    (size === null || (size > 0 && size <= SIZE_RANGE.max))
    && within(parseDecimalInput(draft.outline), WIDTH_RANGE.min, WIDTH_RANGE.max)
    && within(parseDecimalInput(draft.shadow), WIDTH_RANGE.min, WIDTH_RANGE.max)
  );
}

export function hasAnyOverride(draft: StyleDraft): boolean {
  const u = draftToUpdate(draft);
  return Object.values(u).some((v) => v !== null);
}

export function hasStoredOverride(style: StyleSource): boolean {
  return hasAnyOverride(draftFromStyle(style));
}

export interface EffectiveStyle {
  fontName: string;
  fontSize: number;
  bold: boolean;
  italic: boolean;
  outline: number;
  shadow: number;
  primary: string;
  outlineColour: string;
  backColour: string;
}

/** What translated output renders with: each override falls back to the source independently. */
export function resolveEffective(style: StyleSource, draft: StyleDraft): EffectiveStyle {
  const u = draftToUpdate(draft);
  return {
    fontName: u.replacement_font_name ?? style.font_name,
    fontSize: u.replacement_font_size ?? style.font_size,
    bold: u.replacement_bold ?? style.bold,
    italic: u.replacement_italic ?? style.italic,
    outline: u.replacement_outline ?? style.outline,
    shadow: u.replacement_shadow ?? style.shadow,
    primary: u.replacement_primary_colour ?? style.primary_colour ?? DEFAULT_ASS_COLOUR,
    outlineColour: u.replacement_outline_colour ?? style.outline_colour ?? DEFAULT_ASS_COLOUR,
    backColour: u.replacement_back_colour ?? style.back_colour ?? DEFAULT_ASS_COLOUR,
  };
}

// ---------------------------------------------------------------------------
// Shared Source / Replacement grid layout
// ---------------------------------------------------------------------------

export type GridColumnId =
  | 'label' | 'font' | 'size' | 'bold' | 'italic'
  | 'outline' | 'shadow' | 'primary' | 'outlineColour' | 'backColour';

export interface GridColumn {
  id: GridColumnId;
  /** Header text (the three colour columns are Text / Border / Shadow). */
  header: string;
  /** Longer header tooltip, to tell the numeric Shadow from the Shadow colour. */
  hint?: string;
  width: string;
}

/**
 * One column list drives the header, the Source row and the Replacement row, so
 * the three can never drift out of alignment.
 */
export const STYLE_GRID_COLUMNS: readonly GridColumn[] = [
  { id: 'label', header: '', width: '88px' },
  { id: 'font', header: 'Font', width: 'minmax(150px, 1fr)' },
  { id: 'size', header: 'Size', width: '64px' },
  { id: 'bold', header: 'B', hint: 'Bold', width: '36px' },
  { id: 'italic', header: 'I', hint: 'Italic', width: '36px' },
  { id: 'outline', header: 'Outline', hint: 'Outline width', width: '72px' },
  { id: 'shadow', header: 'Shadow', hint: 'Shadow depth', width: '72px' },
  { id: 'primary', header: 'Text', hint: 'Text colour (PrimaryColour)', width: '40px' },
  { id: 'outlineColour', header: 'Border', hint: 'Outline colour (OutlineColour)', width: '40px' },
  { id: 'backColour', header: 'Shadow', hint: 'Shadow colour (BackColour)', width: '40px' },
];

export const STYLE_GRID_TEMPLATE = STYLE_GRID_COLUMNS.map((c) => c.width).join(' ');
/** Narrowest width at which the grid still lays out; below it the panel scrolls, not the page. */
export const STYLE_GRID_MIN_WIDTH = 690;

export type SourceCell =
  | { kind: 'text'; text: string }
  | { kind: 'flag'; on: boolean; label: string }
  | { kind: 'colour'; value: string | null; css: string; title: string };

/** The Source row. Reads only the immutable imported columns — never a replacement. */
export function sourceRowCells(style: StyleSource): Record<Exclude<GridColumnId, 'label'>, SourceCell> {
  const colour = (value: string | null): SourceCell => ({
    kind: 'colour', value, css: assToCssRgba(value), title: describeAssColour(value ?? DEFAULT_ASS_COLOUR),
  });
  return {
    font: { kind: 'text', text: style.font_name },
    size: { kind: 'text', text: formatDecimal(style.font_size) },
    bold: { kind: 'flag', on: style.bold, label: `Source bold: ${style.bold ? 'on' : 'off'}` },
    italic: { kind: 'flag', on: style.italic, label: `Source italic: ${style.italic ? 'on' : 'off'}` },
    outline: { kind: 'text', text: formatDecimal(style.outline) },
    shadow: { kind: 'text', text: formatDecimal(style.shadow) },
    primary: colour(style.primary_colour),
    outlineColour: colour(style.outline_colour),
    backColour: colour(style.back_colour),
  };
}

export function triStateLabel(name: string, value: TriState, source: boolean): string {
  const src = source ? 'on' : 'off';
  if (value === null) return `${name}: inherited from source (${src}). Click to force on.`;
  return value
    ? `${name}: forced on (source ${src}). Click to force off.`
    : `${name}: forced off (source ${src}). Click to inherit.`;
}

// ---------------------------------------------------------------------------
// Live preview
// ---------------------------------------------------------------------------

export interface PreviewCss {
  fontFamily: string;
  fontSize: number;
  fontWeight: number;
  fontStyle: 'normal' | 'italic';
  color: string;
  WebkitTextStroke: string;
  paintOrder: 'stroke fill';
  textShadow: string;
}

export function cssFamily(name: string): string {
  return `"${name.replace(/["\\]/g, '')}", sans-serif`;
}

/**
 * CSS approximation of the effective ASS style. Outline is a centred text-stroke
 * painted under the fill (so its visible width equals the ASS outline), shadow a
 * hard offset in the shadow colour. Sizes are scaled by the same factor the font
 * size is (ASS lengths are in script pixels, like Fontsize).
 */
export function previewCss(eff: EffectiveStyle, minFontPx = 10): PreviewCss {
  const px = Math.max(eff.fontSize, minFontPx);
  const k = eff.fontSize > 0 ? px / eff.fontSize : 1;
  const outline = eff.outline * k;
  const shadow = eff.shadow * k;
  return {
    fontFamily: cssFamily(eff.fontName),
    fontSize: px,
    fontWeight: eff.bold ? 700 : 400,
    fontStyle: eff.italic ? 'italic' : 'normal',
    color: assToCssRgba(eff.primary),
    WebkitTextStroke: outline > 0 ? `${formatDecimal(outline * 2)}px ${assToCssRgba(eff.outlineColour)}` : '0',
    paintOrder: 'stroke fill',
    textShadow: shadow > 0
      ? `${formatDecimal(shadow)}px ${formatDecimal(shadow)}px 0 ${assToCssRgba(eff.backColour)}`
      : 'none',
  };
}
