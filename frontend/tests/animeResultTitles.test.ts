// Run: npm test
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { animeCardRows } from '../src/utils/animeResultTitles.ts';

const base = { title: 'Romaji', title_english: null, title_native: null, year: null };

test('romaji / english / year in order', () => {
  assert.deepEqual(
    animeCardRows({ title: 'Romaji', title_english: 'English', title_native: '日本語', year: 2025 }),
    { primary: 'Romaji', secondary: 'English', year: 2025 },
  );
});

test('missing English falls back to native', () => {
  assert.equal(animeCardRows({ ...base, title_native: '日本語' }).secondary, '日本語');
});

test('secondary identical to primary is omitted', () => {
  assert.equal(animeCardRows({ ...base, title_english: ' romaji ' }).secondary, null);
  assert.equal(animeCardRows({ ...base, title_native: 'Romaji' }).secondary, null);
});

test('English duplicating romaji falls through to native', () => {
  assert.equal(
    animeCardRows({ ...base, title_english: 'Romaji', title_native: '日本語' }).secondary,
    '日本語',
  );
});

test('missing year hides third row, never invented', () => {
  assert.equal(animeCardRows(base).year, null);
  assert.equal(animeCardRows({ ...base, year: 0 }).year, null);
});

test('empty romaji falls back to another title', () => {
  assert.equal(animeCardRows({ ...base, title: '', title_english: 'English' }).primary, 'English');
});

test('AniDB-shaped result (no english field, no year)', () => {
  const r = { title: 'Sousou no Frieren', title_native: '葬送のフリーレン', year: null };
  assert.deepEqual(animeCardRows(r), { primary: 'Sousou no Frieren', secondary: '葬送のフリーレン', year: null });
});

// Layout is one shared component for both providers: clamped to 2 lines, full text in a tooltip.
test('ImportDialog uses one card for every provider with clamp + tooltip', () => {
  const src = readFileSync(new URL('../src/pages/ImportDialog.tsx', import.meta.url), 'utf8');
  assert.equal((src.match(/animeCardRows\(/g) ?? []).length, 1);
  assert.equal((src.match(/lineClamp=\{2\}/g) ?? []).length, 2);
  assert.equal((src.match(/<Tooltip\b/g) ?? []).length >= 2, true);
  assert.match(src, /label=\{rows\.primary\}/);
  assert.match(src, /label=\{rows\.secondary\}/);
  assert.match(src, /size=\{1050\}/);
});
