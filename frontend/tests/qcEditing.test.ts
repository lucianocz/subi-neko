import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { insertionIndex, neighbourAfterRemoval, nextMeta, upsertEvent } from '../src/utils/qcEvents.ts';
import { formatTimeField, nudgeBoundary, parseTimeField, setBoundary, videoTimeToMs } from '../src/utils/qcTime.ts';
import {
  defaultStyle, diffDraft, draftFromEvent, isDirty, isUntouchedNewEvent, mergeSaved, validateNewEvent,
} from '../src/utils/qcDraft.ts';
import { classifyMutationRevision, createLatestGate } from '../src/utils/qcRevision.ts';
import { buildActiveIndex, findActiveIndices } from '../src/utils/qcActiveIndex.ts';
import { WATCHED_WORD_BADGE, WATCHED_ROW_BACKGROUND } from '../src/utils/watchedWords.ts';

const ev = (id: number, line_index: number, start_ms: number, extra: Record<string, unknown> = {}) => ({
  id, line_index, start_ms, end_ms: start_ms + 1000, is_hidden: false, translated_text: `t${id}`, ...extra,
});
// QcEvent has many more fields; the helpers only read these.
const asQc = (e: ReturnType<typeof ev>) => e as never;
const ids = (list: unknown) => (list as { id: number }[]).map((e) => e.id);

test('time formatting and parsing round-trip', () => {
  assert.equal(formatTimeField(754_560), '00:12:34.560');
  assert.equal(formatTimeField(3_723_004), '01:02:03.004');
  assert.equal(parseTimeField('00:12:34.560'), 754_560);
  assert.equal(parseTimeField('12:34.56'), 754_560);
  assert.equal(parseTimeField('1:02:03.004'), 3_723_004);
  assert.equal(parseTimeField('5'), 5000);
  assert.equal(parseTimeField('5.5'), 5500);
  assert.equal(parseTimeField('0:00:61'), null);
  assert.equal(parseTimeField('abc'), null);
  assert.equal(parseTimeField(''), null);
  for (const ms of [0, 10, 59_990, 3_600_000, 86_399_990]) assert.equal(parseTimeField(formatTimeField(ms)), ms);
});

test('timing actions refuse invalid results', () => {
  const t = { startMs: 1000, endMs: 2000 };
  assert.deepEqual(nudgeBoundary(t, 'start', 100), { ok: true, timing: { startMs: 1100, endMs: 2000 } });
  assert.equal(nudgeBoundary(t, 'start', -2000).ok, false);       // negative
  assert.equal(nudgeBoundary(t, 'end', -1000).ok, false);         // end == start
  assert.equal(setBoundary(t, 'start', 2500).ok, false);          // start after end
  assert.equal(setBoundary(t, 'end', 1500).ok, true);
  assert.equal(videoTimeToMs(12.3456), 12346);
});

test('upsert keeps (start_ms, line_index) order and re-sorts on timing change', () => {
  const list = [ev(1, 0, 1000), ev(2, 1, 2000), ev(3, 2, 3000)].map(asQc);
  assert.deepEqual(ids(upsertEvent(list, asQc(ev(1, 0, 2500)), false)), [2, 1, 3]);
  assert.deepEqual(ids(upsertEvent(list, asQc(ev(9, 5, 2000)), false)), [1, 2, 9, 3]);
  assert.deepEqual(ids(upsertEvent(list, asQc(ev(9, 0, 2000)), false)), [1, 9, 2, 3]);   // lower line_index first
  assert.equal(upsertEvent(list, asQc(ev(2, 1, 2000, { translated_text: 'new' })), false).length, 3);
});

test('hidden events leave the list unless hidden are shown', () => {
  const list = [ev(1, 0, 1000), ev(2, 1, 2000)].map(asQc);
  const hidden = ev(2, 1, 2000, { is_hidden: true });
  assert.deepEqual(ids(upsertEvent(list, asQc(hidden), false)), [1]);
  assert.equal(upsertEvent(list, asQc(hidden), true).length, 2);
  assert.equal(insertionIndex([], asQc(ev(1, 0, 5))), 0);
});

test('meta counters follow hide / restore / create', () => {
  const meta = { event_count: 2, total_count: 3, hidden_count: 1 };
  const evs = [ev(1, 0, 1)];
  assert.deepEqual(nextMeta(meta, evs, { is_hidden: false }, { is_hidden: true }, false),
    { event_count: 1, total_count: 3, hidden_count: 2 });
  // restore of a hidden event that is not in the (hidden-excluded) list
  assert.deepEqual(nextMeta(meta, evs, undefined, { is_hidden: false }, false),
    { event_count: 1, total_count: 3, hidden_count: 0 });
  assert.deepEqual(nextMeta(meta, evs, undefined, { is_hidden: false }, true),
    { event_count: 1, total_count: 4, hidden_count: 1 });
});

test('neighbour selection after removal', () => {
  assert.equal(neighbourAfterRemoval(['a', 'c'], 1), 'a');
  assert.equal(neighbourAfterRemoval(['a', 'b', 'c'], 1), 'c');
  assert.equal(neighbourAfterRemoval([], 0), null);
});

test('active index follows the edited timing immediately', () => {
  const list = [ev(1, 0, 1000), ev(2, 1, 5000)].map(asQc);
  assert.deepEqual(findActiveIndices(buildActiveIndex(list), 6200), []);
  const edited = upsertEvent(list, asQc(ev(2, 1, 5000, { end_ms: 7000 })), false);
  assert.deepEqual(findActiveIndices(buildActiveIndex(edited), 6200), [1]);
});

test('draft diff sends only changed fields; null text counts as empty', () => {
  const e = asQc(ev(1, 0, 1000, { translated_text: null }));
  const d = draftFromEvent(e);
  assert.deepEqual(diffDraft(d, e), {});
  assert.equal(isDirty(d, e), false);
  assert.deepEqual(diffDraft({ ...d, text: 'x', endMs: 2500 }, e), { translated_text: 'x', end_ms: 2500 });
  assert.equal(isDirty({ ...d, startMs: 990 }, e), true);
  assert.equal(isDirty({ ...d, id: 99, text: 'x' }, e), false);   // draft belongs to another event
});

test('mergeSaved keeps text typed while the request was in flight', () => {
  const saved = asQc(ev(1, 0, 1000, { translated_text: 'abc', start_ms: 1010 }));
  const sent = { translated_text: 'abc', start_ms: 1004 };
  const same = mergeSaved({ id: 1, text: 'abc', startMs: 1004, endMs: 2000 }, sent, saved);
  assert.deepEqual(same, { id: 1, text: 'abc', startMs: 1010, endMs: 2000 });   // server's quantized start wins
  const typed = mergeSaved({ id: 1, text: 'abcd', startMs: 1004, endMs: 2000 }, sent, saved);
  assert.equal(typed.text, 'abcd');
});

test('new manual event validation and defaults', () => {
  const ok = { text: 'Sign', startMs: 1000, endMs: 3000, style: 'Default', speaker: '' };
  assert.deepEqual(validateNewEvent(ok, ['Default', 'Sign']), []);
  assert.ok(validateNewEvent({ ...ok, text: '  ' }, ['Default']).includes('Text is required.'));
  assert.ok(validateNewEvent({ ...ok, style: '' }, ['Default']).includes('Choose a style.'));
  assert.ok(validateNewEvent({ ...ok, style: 'Nope' }, ['Default']).includes('Choose a style.'));
  assert.ok(validateNewEvent({ ...ok, endMs: 1000 }, ['Default']).includes('Start must be before end.'));
  assert.ok(validateNewEvent({ ...ok, startMs: Number.NaN }, ['Default']).includes('Start is required.'));
  assert.equal(defaultStyle(['Sign', 'default']), 'default');
  assert.equal(defaultStyle(['Sign', 'Main']), '');
  assert.equal(isUntouchedNewEvent({ ...ok, text: '' }, { ...ok, text: '' }), true);
  assert.equal(isUntouchedNewEvent({ ...ok, text: 'x' }, { ...ok, text: '' }), false);
});

test('revision classification: own edit vs external change', () => {
  assert.deepEqual(classifyMutationRevision(5, 6), { kind: 'ours', known: 6 });
  assert.deepEqual(classifyMutationRevision(5, 5), { kind: 'ours', known: 5 });   // no-op
  assert.deepEqual(classifyMutationRevision(5, 7), { kind: 'external' });
});

test('latest-wins gate: an older preview never overwrites a newer one', () => {
  const gate = createLatestGate();
  const a = gate.begin();
  const b = gate.begin();
  assert.equal(gate.isCurrent(a), false);
  assert.equal(gate.isCurrent(b), true);
  // out-of-order completion: B applies, A (resolving later) is dropped
  const applied: string[] = [];
  for (const [token, name] of [[b, 'B'], [a, 'A']] as const) if (gate.isCurrent(token)) applied.push(name);
  assert.deepEqual(applied, ['B']);
});

test('watched-word visuals: one shared definition used by both editors', () => {
  assert.deepEqual({ ...WATCHED_WORD_BADGE }, { size: 'xs', color: 'yellow', variant: 'light' });
  assert.equal(WATCHED_ROW_BACKGROUND, 'rgba(250, 176, 5, 0.08)');
  const read = (p: string) => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8');
  assert.match(read('pages/SubtitleEditorDialog.tsx'), /<WatchedWordBadge /);
  assert.match(read('components/qc/QcEventDetail.tsx'), /<WatchedWordBadge /);
  assert.match(read('components/WatchedWordBadge.tsx'), /\{\.\.\.WATCHED_WORD_BADGE\}/);
  assert.doesNotMatch(read('components/qc/QcEventList.tsx'), /grape/);
});

test('branding draft: round-trips through the shared QC time helpers', async () => {
  const b = await import('../src/utils/qcBranding.ts');
  const saved = { enabled: true, template_filename: 'final.ass', start_offset_ms: 1_200_000, scale_to_script_playres: false };
  const draft = b.draftFromBranding({ ...saved, output_revision: 3 });
  assert.deepEqual(draft, { enabled: true, template: 'final.ass', start: '00:20:00.000', scale: false });
  assert.deepEqual(b.parseBrandingDraft(draft), { ok: true, body: saved });
  assert.equal(b.isBrandingDirty(draft, saved), false);
  assert.equal(b.isBrandingDirty({ ...draft, start: '20:00' }, saved), false);   // same value, other spelling
  assert.equal(b.isBrandingDirty({ ...draft, start: '20:01' }, saved), true);
  assert.equal(b.isBrandingDirty({ ...draft, enabled: false }, saved), true);
  assert.equal(b.isBrandingDirty({ ...draft, scale: true }, saved), true);
  assert.deepEqual(b.parseBrandingDraft({ ...draft, scale: true }), { ok: true, body: { ...saved, scale_to_script_playres: true } });
});

test('branding draft validation', async () => {
  const b = await import('../src/utils/qcBranding.ts');
  assert.equal(b.parseBrandingDraft({ enabled: true, template: 'a.ass', start: 'nope', scale: false }).ok, false);
  assert.equal(b.parseBrandingDraft({ enabled: true, template: null, start: '0', scale: false }).ok, false);
  // disabled keeps its config even without a template
  assert.deepEqual(b.parseBrandingDraft({ enabled: false, template: null, start: '1.5', scale: false }),
    { ok: true, body: { enabled: false, template_filename: null, start_offset_ms: 1500, scale_to_script_playres: false } });
});

test('branding: font refresh decision, indicator and template choices', async () => {
  const b = await import('../src/utils/qcBranding.ts');
  const on = { enabled: true, template_filename: 'a.ass', start_offset_ms: 0, scale_to_script_playres: false };
  // toggling the scale option never changes font requirements
  assert.equal(b.brandingNeedsFontRefresh(on, { ...on, scale_to_script_playres: true }), false);
  assert.equal(b.brandingNeedsFontRefresh(on, { ...on, start_offset_ms: 5000 }), false);
  assert.equal(b.brandingNeedsFontRefresh(on, { ...on, template_filename: 'b.ass' }), true);
  assert.equal(b.brandingNeedsFontRefresh(on, { ...on, enabled: false }), true);
  assert.equal(b.brandingNeedsFontRefresh({ ...on, enabled: false }, { ...on, enabled: false, template_filename: 'b.ass' }), false);
  assert.equal(b.brandingIndicator({ enabled: true }), 'on');
  assert.equal(b.brandingIndicator({ enabled: false }), 'off');
  assert.equal(b.brandingIndicator(undefined), 'off');
  assert.deepEqual(b.templateOptions(['a.ass'], 'gone.ass'), ['gone.ass', 'a.ass']);
  assert.deepEqual(b.templateOptions(['a.ass'], 'a.ass'), ['a.ass']);
});

test('saving branding adopts the response revision like an own mutation', () => {
  assert.deepEqual(classifyMutationRevision(7, 8), { kind: 'ours', known: 8 });
  assert.deepEqual(classifyMutationRevision(7, 9), { kind: 'external' });
});
