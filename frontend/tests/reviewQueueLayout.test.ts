// Static regression checks for the Review queue dialog (no DOM in this test runner).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const tsx = readFileSync(new URL('../src/pages/ReviewQueueDialog.tsx', import.meta.url), 'utf8');
const css = readFileSync(new URL('../src/pages/reviewQueue.css', import.meta.url), 'utf8');

test('no selected / active card state remains', () => {
  for (const gone of ['activeIndex', 'setActiveIndex', 'clampedIndex', 'onActivate', 'onResolved',
    'active=', 'rowRef', 'useRef', 'useEffect', 'scrollIntoView', 'stopPropagation']) {
    assert.ok(!tsx.includes(gone), `still references ${gone}`);
  }
  assert.doesNotMatch(tsx, /blue-[57]/);
});

test('the card has no click handler, pointer cursor, outline or tabIndex', () => {
  const card = tsx.slice(tsx.indexOf('<Stack gap={6}'), tsx.indexOf('</Stack>', tsx.indexOf('<Stack gap={6}')));
  assert.doesNotMatch(card.split('\n')[0], /onClick|tabIndex|role=/);
  assert.doesNotMatch(css, /cursor:\s*pointer|outline|blue/);
});

test('resolve actions are still wired to the existing mutations', () => {
  assert.match(tsx, /resolve\.mutateAsync\(\{ fileId: item\.file_id, issueId: item\.id \}\)/);
  assert.match(tsx, /save\.mutateAsync\(\{[^}]*translatedText: draft/s);
  assert.ok(tsx.includes('Resolve all warnings'));
  assert.ok(tsx.includes('Save & resolve'));
  assert.match(tsx, /Could not resolve the issue\./);
  assert.match(tsx, /Could not save the translation\./);
});

test('issue text is rendered verbatim (no normalisation of ASS markup)', () => {
  assert.match(tsx, /\{item\.message\}/);
  assert.match(tsx, /\{item\.source_text\}/);
  assert.match(tsx, /value=\{draft\}/);
  assert.doesNotMatch(tsx, /(message|source_text|draft)\.(replace|split|slice|substring|normalize)/);
});

test('message, source and translation wrap long unbroken strings, keeping line breaks', () => {
  assert.match(css, /\.rq-text\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(css, /\.rq-text\s*\{[^}]*white-space:\s*pre-wrap/s);
  assert.match(css, /\.rq-text\s*\{[^}]*min-width:\s*0/s);
  assert.match(css, /\.rq-text\s*\{[^}]*max-width:\s*100%/s);
  assert.match(tsx, /className="rq-text">\{item\.message\}/);
  assert.match(tsx, /className="rq-text">\s*\{item\.source_text\}/);
  assert.match(tsx, /classNames=\{\{ input: 'rq-textarea' \}\}/);
  assert.match(css, /\.rq-textarea\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  // Descriptions are never truncated.
  assert.doesNotMatch(tsx, /<Text[^>]*truncate[^>]*>\s*\{item\.(message|source_text)/);
});

test('card and metadata row can shrink; list scrolls vertically only', () => {
  assert.match(css, /\.rq-card\s*\{[^}]*min-width:\s*0/s);
  assert.match(css, /\.rq-meta\s*\{[^}]*min-width:\s*0/s);
  assert.match(tsx, /<ScrollArea\.Autosize mah="65vh" scrollbars="y">/);
});
