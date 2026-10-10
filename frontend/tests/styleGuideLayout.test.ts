// Static regression checks for the Style guide dialog layout (no DOM in this test runner).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const css = readFileSync(new URL('../src/pages/styleGuide.css', import.meta.url), 'utf8');
const dialog = readFileSync(new URL('../src/pages/StyleGuideDialog.tsx', import.meta.url), 'utf8');
const characters = readFileSync(new URL('../src/pages/CharactersTab.tsx', import.meta.url), 'utf8');

function colsOf(cls: string): string {
  const m = css.match(new RegExp(`\\.${cls}\\s*\\{\\s*--sgd-cols:\\s*([^;]+);`));
  assert.ok(m, `no --sgd-cols for .${cls}`);
  return m[1].replace(/\s+/g, ' ').trim();
}

/** Split a grid-template-columns value into top-level tracks (respecting parentheses). */
function tracks(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = '';
  for (const ch of value) {
    if (ch === '(') depth++;
    if (ch === ')') depth--;
    if (ch === ' ' && depth === 0) {
      if (cur) out.push(cur);
      cur = '';
    } else cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

const TABS = { glossary: 7, tm: 5, char: 5, speaker: 6, extra: 3, pair: 3 };

for (const [cls, count] of Object.entries(TABS)) {
  test(`.sgd-${cls}: explicit ${count} tracks, flexible ones are minmax(0|px, …)`, () => {
    const t = tracks(colsOf(`sgd-${cls}`));
    assert.equal(t.length, count);
    for (const track of t) {
      // A bare `1fr` / `auto` track has a min-content minimum and lets long text widen it.
      assert.ok(!/^[\d.]+fr$/.test(track), `bare fr track ${track}`);
      assert.notEqual(track, 'auto');
      if (track.includes('fr')) assert.match(track, /^minmax\((0|\d+px), [\d.]+fr\)$/);
    }
  });
}

test('translation memory: source and translation take the flexible space, rest stay compact', () => {
  const t = tracks(colsOf('sgd-tm'));
  assert.match(t[0], /fr\)$/);
  assert.match(t[1], /fr\)$/);
  for (const compact of t.slice(2)) assert.match(compact, /^\d+px$/);
});

test('glossary: category/vocative/origin/delete are fixed, text columns flexible', () => {
  const t = tracks(colsOf('sgd-glossary'));
  assert.deepEqual(
    t.map((x) => (x.includes('fr') ? 'flex' : 'fixed')),
    ['flex', 'flex', 'fixed', 'fixed', 'flex', 'fixed', 'fixed'],
  );
});

test('header and rows share one grid definition', () => {
  assert.match(css, /\.sgd-head,\s*\.sgd-row\s*\{[^}]*grid-template-columns:\s*var\(--sgd-cols\)/s);
  for (const cls of ['glossary', 'tm', 'char']) {
    assert.match(dialog + characters, new RegExp(`sgd-head sgd-${cls}`));
    assert.match(dialog + characters, new RegExp(`sgd-row sgd-${cls}`));
  }
});

test('grid children can shrink and long strings wrap visually', () => {
  assert.match(css, /\.sgd-row > \*[^{]*\{\s*min-width:\s*0/);
  assert.match(css, /\.sgd-wrap\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(css, /\.sgd-wrap\s*\{[^}]*white-space:\s*pre-wrap/s);
  assert.match(css, /\.sgd-wrap\s*\{[^}]*min-width:\s*0/s);
});

test('TM source is rendered verbatim in a wrapping cell', () => {
  assert.match(dialog, /className="sgd-wrap[^"]*"[^>]*>\{entry\.source_text\}<\/Text>/);
  // No transformation of the source (no replace/split/slice/insertion).
  assert.doesNotMatch(dialog, /entry\.source_text\.(replace|split|slice|substring)/);
});

test('narrow viewport collapses to a controlled two-column layout, not squeezed columns', () => {
  const media = css.slice(css.indexOf('@media'));
  assert.match(media, /--sgd-cols:\s*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(media, /\.sgd-head\s*\{\s*display:\s*none/);
});

test('zebra striping is CSS-only, subtle and applied to every repeated list', () => {
  assert.match(css, /\.sgd-zebra > :nth-child\(even\)\s*\{[^}]*background-color:\s*var\(--sgd-stripe\)/s);
  const alphas = [...css.matchAll(/--sgd-stripe:\s*rgba\([^)]*,\s*([\d.]+)\)/g)].map((m) => Number(m[1]));
  assert.ok(alphas.length >= 2);
  for (const a of alphas) assert.ok(a > 0 && a <= 0.05, `stripe alpha ${a} too strong`);

  // Address pairs, characters, unmapped speakers, extras, glossary and TM all sit in a zebra list.
  assert.equal((dialog.match(/className="sgd-zebra"/g) ?? []).length, 3);
  assert.equal((characters.match(/className="sgd-zebra"/g) ?? []).length, 3);
  // A character block (header line + its speakers) is a single child → single stripe.
  assert.match(characters, /<div className="sgd-block">/);
});

test('characters: per-row dividers replaced by striping, editing controls kept', () => {
  assert.doesNotMatch(characters, /Divider/);
  for (const needle of ['updateCharacter.mutateAsync', 'updateVoice.mutate', 'updateSpeaker.mutateAsync',
    'placeholder="Voice note"', 'placeholder="Register"', 'GENDER_OPTIONS', 'Retranslate']) {
    assert.ok(characters.includes(needle), needle);
  }
});

test('style bible keeps the address-pair controls and values', () => {
  assert.match(dialog, /updatePair\.mutate\(\{ pairId: pair\.id, mode: mode as typeof pair\.mode \}\)/);
  assert.ok(dialog.includes('data={MODE_OPTIONS}'));
});

test('no JS layout measurement was introduced', () => {
  for (const src of [dialog, characters]) {
    assert.doesNotMatch(src, /ResizeObserver|MutationObserver|getBoundingClientRect|offsetWidth/);
  }
});
