import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { replayFromStart, replaySeekMs } from '../src/utils/qcTime.ts';

const fakeVideo = (play: () => Promise<void> | void = () => Promise.resolve()) => {
  const v = { currentTime: 99, plays: 0, play() { v.plays += 1; return play(); } };
  return v;
};

test('seek target is Start − 500 ms, clamped at zero', () => {
  assert.equal(replaySeekMs(10_000), 9_500);
  assert.equal(replaySeekMs(500), 0);
  assert.equal(replaySeekMs(200), 0);
  assert.equal(replaySeekMs(0), 0);
});

test('replay seeks (seconds) and plays; uses whatever Start it is given (unsaved edits)', () => {
  const v = fakeVideo();
  assert.equal(replayFromStart(v as never, 12_340), true);
  assert.equal(v.currentTime, 11.84);
  assert.equal(v.plays, 1);
});

test('repeated clicks seek back and play again, even mid-playback', () => {
  const v = fakeVideo();
  replayFromStart(v as never, 5_000);
  v.currentTime = 8; // playback moved well past the event
  replayFromStart(v as never, 5_000);
  assert.equal(v.currentTime, 4.5);
  assert.equal(v.plays, 2);
});

test('missing video or invalid Start is a safe no-op', () => {
  assert.equal(replayFromStart(null, 1000), false);
  assert.equal(replayFromStart(undefined, 1000), false);
  const v = fakeVideo();
  assert.equal(replayFromStart(v as never, Number.NaN), false);
  assert.equal(v.currentTime, 99);
  assert.equal(v.plays, 0);
});

test('rejected or throwing play() does not escape', async () => {
  const unhandled: unknown[] = [];
  const on = (e: unknown) => unhandled.push(e);
  process.on('unhandledRejection', on);
  replayFromStart(fakeVideo(() => Promise.reject(new Error('blocked'))) as never, 1000);
  assert.equal(replayFromStart(fakeVideo(() => { throw new Error('sync'); }) as never, 1000), true);
  await new Promise((r) => setTimeout(r, 20));
  process.off('unhandledRejection', on);
  assert.deepEqual(unhandled, []);
});

test('replay adds no playback state: no pause, timer, End boundary or event selection', () => {
  const fn = readFileSync(new URL('../src/utils/qcTime.ts', import.meta.url), 'utf8')
    .split('export function replayFromStart')[1];
  assert.doesNotMatch(fn, /pause|setTimeout|setInterval|loop|endMs|timeupdate/);
  const page = readFileSync(new URL('../src/pages/FinalQcPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /const handleReplay = useCallback\(\(startMs: number\) => \{ replayFromStart\(videoRef\.current, startMs\); \}, \[\]\)/);
});

test('detail panel: Replay sits between Duration and CPS and sends the live Start', () => {
  const src = readFileSync(new URL('../src/components/qc/QcEventDetail.tsx', import.meta.url), 'utf8');
  assert.match(src, /\{replay\}\s*\{cps\}/);
  assert.match(src, /aria-label="Replay event"/);
  assert.match(src, /<Tooltip label="Replay event"/);
  assert.match(src, /onClick=\{\(\) => onReplay\(timing\.startMs\)\}/);
  assert.match(src, /<Play size/);
});

test('Hide / Restore lives in the right panel after the QA list, unconditionally', () => {
  const src = readFileSync(new URL('../src/components/qc/QcEventDetail.tsx', import.meta.url), 'utf8');
  const side = src.slice(src.indexOf('data-testid="qc-side"'));
  const qa = side.indexOf('data-testid="qc-qa"');
  const hide = side.indexOf('data-testid="qc-hide-toggle"');
  assert.ok(qa > 0 && hide > qa, 'hide toggle follows the QA block');
  assert.match(side.slice(qa, hide), /<\/div>\s*<\/div>\s*<Group justify="flex-end"/); // closes qc-qa and the bordered .qc-meta first
  assert.match(side, /onClick=\{onToggleHidden\}/);
  assert.match(side, /event\.is_hidden \? 'Restore event' : 'Hide event'/);
  // Not inside QcIssuesPanel (which returns early with zero issues) nor the timing editor.
  assert.doesNotMatch(src.slice(0, src.indexOf('data-testid="qc-side"')), /qc-hide-toggle/);
});
