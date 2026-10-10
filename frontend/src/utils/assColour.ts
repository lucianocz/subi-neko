/**
 * ASS colour helpers. ASS stores colours as `&HAABBGGRR&` where AA is an
 * *inverted* alpha (00 = opaque, FF = fully transparent) and the channel order is
 * BGR. CSS/Mantine use `#RRGGBBAA` with a conventional alpha (FF = opaque).
 *
 * The ASS string is the single canonical representation in state and on the wire;
 * everything here converts losslessly (256 alpha levels) in both directions.
 */

export interface AssColour {
  r: number;
  g: number;
  b: number;
  /** ASS alpha: 0 = opaque … 255 = fully transparent. */
  a: number;
}

/** Colour libass/the backend fall back to when a style carries none. */
export const DEFAULT_ASS_COLOUR = '&H00FFFFFF&';

const ASS_RE = /^&[Hh]([0-9a-fA-F]{6}|[0-9a-fA-F]{8})&?$/;
const HEX_RE = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

const h2 = (n: number) => n.toString(16).toUpperCase().padStart(2, '0');

export function parseAssColour(value: string | null | undefined): AssColour | null {
  const m = value ? ASS_RE.exec(value.trim()) : null;
  if (!m) return null;
  const hex = m[1].padStart(8, '0');
  return {
    a: parseInt(hex.slice(0, 2), 16),
    b: parseInt(hex.slice(2, 4), 16),
    g: parseInt(hex.slice(4, 6), 16),
    r: parseInt(hex.slice(6, 8), 16),
  };
}

export function formatAssColour({ r, g, b, a }: AssColour): string {
  return `&H${h2(a)}${h2(b)}${h2(g)}${h2(r)}&`;
}

/** `#rrggbbaa` (CSS alpha) for the Mantine ColorPicker's `hexa` format. */
export function assToHexa(value: string | null | undefined): string {
  const c = parseAssColour(value) ?? parseAssColour(DEFAULT_ASS_COLOUR)!;
  return `#${h2(c.r)}${h2(c.g)}${h2(c.b)}${h2(255 - c.a)}`.toLowerCase();
}

/** `#RRGGBB`, `#RGB` or `#RRGGBBAA` (CSS alpha) → canonical ASS, or null if malformed. */
export function parseColourInput(text: string): string | null {
  const m = HEX_RE.exec(text.trim());
  if (!m) return null;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split('').map((ch) => ch + ch).join('');
  if (hex.length === 6) hex += 'ff';
  const n = (i: number) => parseInt(hex.slice(i, i + 2), 16);
  return formatAssColour({ r: n(0), g: n(2), b: n(4), a: 255 - n(6) });
}

/** CSS colour with alpha (3 decimals is enough to keep every 1/255 step distinct). */
export function assToCssRgba(value: string | null | undefined): string {
  const c = parseAssColour(value) ?? parseAssColour(DEFAULT_ASS_COLOUR)!;
  const alpha = Math.round(((255 - c.a) / 255) * 1000) / 1000;
  return `rgba(${c.r}, ${c.g}, ${c.b}, ${alpha})`;
}

/** Tooltip text: shows the exact stored value, including alpha. */
export function describeAssColour(value: string | null | undefined): string {
  const c = parseAssColour(value);
  if (!c) return 'Default (white)';
  const rgb = `#${h2(c.r)}${h2(c.g)}${h2(c.b)}`;
  const opacity = Math.round(((255 - c.a) / 255) * 100);
  return `${rgb} · opacity ${opacity}% · ${formatAssColour(c)}`;
}

const round2 = (x: number) => Math.round(x * 100) / 100;

/**
 * Fold a ColorPicker `onChange` value into the current colour without drift.
 *
 * The picker works in HSV with a 2-decimal alpha, so a pure alpha drag can nudge
 * RGB by ±1 and a pure RGB drag can nudge alpha by one level. Whichever component
 * the user did *not* change keeps its exact previous value.
 */
export function mergePickerValue(current: string | null | undefined, hexa: string): string | null {
  const next = parseColourInput(hexa);
  if (!next) return null;
  const cur = parseAssColour(current) ?? parseAssColour(DEFAULT_ASS_COLOUR)!;
  const n = parseAssColour(next)!;
  const rgbSame = Math.max(Math.abs(n.r - cur.r), Math.abs(n.g - cur.g), Math.abs(n.b - cur.b)) <= 1;
  // The alpha the picker would emit for an untouched current value.
  const pickerAlphaForCurrent = Math.round(round2((255 - cur.a) / 255) * 255);
  const alphaSame = 255 - n.a === pickerAlphaForCurrent;
  return formatAssColour({
    r: rgbSame ? cur.r : n.r,
    g: rgbSame ? cur.g : n.g,
    b: rgbSame ? cur.b : n.b,
    a: alphaSame ? cur.a : n.a,
  });
}
