// Three-row anime search card content, shared by AniDB and AniList results.
export interface AnimeResultTitleInput {
  title: string;
  title_english?: string | null;
  title_native: string | null;
  year: number | null;
}

export interface AnimeCardRows {
  primary: string;
  secondary: string | null;
  year: number | null;
}

const clean = (s: string | null | undefined): string => (s ?? '').trim();
const same = (a: string, b: string): boolean => a.toLowerCase() === b.toLowerCase();

export function animeCardRows(r: AnimeResultTitleInput): AnimeCardRows {
  // Romaji is the main title; fall back so the card never renders empty.
  const primary = clean(r.title) || clean(r.title_english) || clean(r.title_native);
  const english = clean(r.title_english);
  const native = clean(r.title_native);

  let secondary: string | null = null;
  if (english && !same(english, primary)) secondary = english;
  else if (native && !same(native, primary)) secondary = native;

  return { primary, secondary, year: r.year ? r.year : null };
}
