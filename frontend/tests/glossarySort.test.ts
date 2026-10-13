import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { sortGlossaryTerms } from '../src/utils/glossarySort.ts';

const t = (id: number, source_term: string, target_term = 'x') => ({ id, source_term, target_term });

test('alphabetical by original term, case-insensitive', () => {
  const out = sortGlossaryTerms([t(1, 'zeta'), t(2, 'Alpha'), t(3, 'beta'), t(4, 'Gamma'), t(5, 'alpha2')]);
  assert.deepEqual(out.map((x) => x.source_term), ['Alpha', 'alpha2', 'beta', 'Gamma', 'zeta']);
});

test('locale-aware: accented letters sort with their base letter', () => {
  const out = sortGlossaryTerms([t(1, 'Zoe'), t(2, 'Émile'), t(3, 'Eve'), t(4, 'Ada')]);
  assert.deepEqual(out.map((x) => x.source_term), ['Ada', 'Émile', 'Eve', 'Zoe']);
});

test('deterministic for equal names (translation, then id) regardless of input order', () => {
  const a = [t(3, 'Sword', 'meč'), t(1, 'sword', 'meč'), t(2, 'SWORD', 'čepel')];
  const b = [a[2], a[0], a[1]];
  assert.deepEqual(sortGlossaryTerms(a).map((x) => x.id), sortGlossaryTerms(b).map((x) => x.id));
});

test('does not mutate the input (query cache) and re-sorts after add / edit / delete', () => {
  const cache = Object.freeze([t(1, 'b'), t(2, 'a')]);
  const sorted = sortGlossaryTerms(cache);
  assert.notEqual(sorted, cache);
  assert.deepEqual(cache.map((x) => x.id), [1, 2]);
  const added = sortGlossaryTerms([...cache, t(3, 'AA')]);
  assert.deepEqual(added.map((x) => x.id), [2, 3, 1]);
  const edited = sortGlossaryTerms(cache.map((x) => (x.id === 2 ? { ...x, source_term: 'z' } : x)));
  assert.deepEqual(edited.map((x) => x.id), [1, 2]);
  assert.deepEqual(sortGlossaryTerms(added.filter((x) => x.id !== 3)).map((x) => x.id), [2, 1]);
});

test('Glossary tab renders the sorted, memoized list', () => {
  const src = readFileSync(new URL('../src/pages/StyleGuideDialog.tsx', import.meta.url), 'utf8');
  assert.match(src, /useMemo\(\(\) => \(rawTerms \? sortGlossaryTerms\(rawTerms\) : undefined\), \[rawTerms\]\)/);
  assert.match(src, /terms\.map\(\(term\) => \(\s*<GlossaryRow key=\{term\.id\}/);
});
