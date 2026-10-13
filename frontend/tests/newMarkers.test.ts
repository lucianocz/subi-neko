import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { NEW_MARKER_LABEL, countNew, filterNew } from '../src/utils/newMarkers.ts';
import { sortGlossaryTerms } from '../src/utils/glossarySort.ts';

const css = readFileSync(new URL('../src/pages/styleGuide.css', import.meta.url), 'utf8');
const dialog = readFileSync(new URL('../src/pages/StyleGuideDialog.tsx', import.meta.url), 'utf8');
const hooks = readFileSync(new URL('../src/hooks/useStyleGuide.ts', import.meta.url), 'utf8');

const item = (id: number, is_new: boolean, source_term = `t${id}`) => ({ id, is_new, source_term, target_term: 'x' });

test('countNew counts every flagged entry; filterNew keeps only those, preserving order', () => {
  const items = [item(1, true), item(2, false), item(3, true)];
  assert.equal(countNew(items), 2);
  assert.deepEqual(filterNew(items, true).map((x) => x.id), [1, 3]);
  assert.deepEqual(filterNew(items, false).map((x) => x.id), [1, 2, 3]);
  assert.equal(countNew([]), 0);
});

test('filtering never mutates the input array or its items (query-cache safe)', () => {
  const items = Object.freeze([Object.freeze(item(1, true)), Object.freeze(item(2, false))]);
  const out = filterNew(items, false);
  assert.notEqual(out, items); // a copy even when the filter is off
  filterNew(items, true);
  assert.equal(items.length, 2);
  assert.equal(items[0].is_new, true);
});

test('the count is independent of the filter and of the sort order', () => {
  const items = [item(1, true, 'zeta'), item(2, false, 'alpha'), item(3, true, 'beta')];
  assert.equal(countNew(items), countNew(filterNew(items, true)));
  const sorted = sortGlossaryTerms(items);
  assert.deepEqual(filterNew(sorted, true).map((x) => x.source_term), ['beta', 'zeta']); // still by Original Term
});

test('types expose is_new for glossary terms and address pairs; it is never sent in writes', () => {
  assert.equal((hooks.match(/is_new: boolean;/g) ?? []).length, 2);
  assert.doesNotMatch(hooks, /is_new\??:[^;]*;[^}]*mutationFn/s);
  assert.doesNotMatch(dialog, /is_new:/); // the dialog never constructs/sends the flag
});

test('star is shown only for flagged entries, right after the identity text, with the tooltip', () => {
  assert.equal(NEW_MARKER_LABEL, 'New or updated since last translation');
  assert.match(dialog, /\{term\.source_term\}\s*<NewMarker show=\{term\.is_new\} \/>/);
  assert.match(dialog, /\{pair\.speaker_name\} → \{pair\.addressee_name\}\s*<NewMarker show=\{pair\.is_new\} \/>/);
  assert.match(dialog, /if \(!show\) return null;/);
  assert.match(dialog, /★/);
  // Not a Badge/Pill and no new grid column.
  assert.doesNotMatch(dialog, /<(Badge|Pill)[^>]*>\s*★/);
});

test('star is inline (wraps with the text: no extra height/width) and bold warm yellow in both themes', () => {
  const rule = css.match(/\.sgd-new\s*\{([^}]*)\}/s)?.[1] ?? '';
  assert.match(rule, /font-weight:\s*700/);
  assert.match(rule, /yellow/);
  assert.doesNotMatch(rule, /display:\s*(block|flex|grid)|width|height/);
  assert.match(css, /data-mantine-color-scheme='dark'\]\s*\.sgd-new\s*\{[^}]*yellow/s);
  // Long names keep their wrapping behaviour; the star is part of the same text node's flow.
  assert.match(css, /\.sgd-wrap\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(dialog, /className="sgd-wrap sgd-cell sgd-full"[^>]*>\s*\{term\.source_term\}/);
});

test('both tabs get an "Only new (N)" toggle driven by countNew over ALL entries, with empty states', () => {
  assert.match(dialog, /label=\{`Only new \(\$\{count\}\)`\}/);
  assert.match(dialog, /count=\{countNew\(terms\)\}/);
  assert.match(dialog, /count=\{countNew\(data\.address_pairs\)\}/);
  assert.match(dialog, /filterNew\(terms \?\? \[\], onlyNew\)/);
  assert.match(dialog, /visibleTerms\.map/);
  assert.match(dialog, /visiblePairs\.map/);
  assert.match(dialog, /No new or updated glossary terms\./);
  assert.match(dialog, /No new or updated address pairs\./);
});

test('glossary toggle sits in the existing header (no new toolbar row) and stays reachable when narrow', () => {
  assert.match(dialog, /sgd-head sgd-glossary sgd-head-filter/);
  assert.match(css, /\.sgd-head\.sgd-head-filter\s*\{[^}]*display:\s*block/s);
});
