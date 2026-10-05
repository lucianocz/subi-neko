/** ASS text → single-line display text (list excerpts): drops `{...}` override
 * blocks and turns hard/soft line breaks and hard spaces into spaces. */
export function plainAssText(text: string | null | undefined): string {
  if (!text) return '';
  return text
    .replace(/\{[^}]*\}/g, '')
    .replace(/\\[Nn]/g, ' ')
    .replace(/\\h/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** `h:mm:ss.mmm` (or `m:ss.mmm` below an hour). */
export function formatMs(ms: number): string {
  const sign = ms < 0 ? '-' : '';
  const abs = Math.abs(Math.round(ms));
  const h = Math.floor(abs / 3_600_000);
  const m = Math.floor(abs / 60_000) % 60;
  const s = Math.floor(abs / 1000) % 60;
  const milli = abs % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${sign}${h > 0 ? `${h}:${pad(m)}` : String(m)}:${pad(s)}.${pad(milli, 3)}`;
}
