import assert from 'node:assert/strict';
import { test } from 'node:test';
import { advanceWorkProgress, assignFile, episodeKey, operationStatus, prepareAssignments, publicationLines, uploadFileIds } from '../src/utils/publicationWorkflow.ts';
import type { WebFile } from '../src/utils/webPublishing.ts';
import type { WebResult } from '../src/hooks/useWebPublishing.ts';
const episodes = [1, 2, 3].map(n => ({ id: n, tmdb_episode_id: `tmdb-${n}`, episode_number: n, name_english: `Episode ${n}` }));
const file = (id: number, n: number, status = 'accepted'): WebFile => ({ id, filename: `file-${id}.mkv`, episode_number: n, status,
  mapping: null, locked: false, published_at: null, current_sha256: null, published_sha256: null, render_error: null });
const files = [file(10, 1), file(20, 2, 'processing')];

test('all metadata episodes are present regardless of local files or acceptance', () => {
  const a = prepareAssignments(episodes, files);
  assert.deepEqual(Object.keys(a), ['tmdb-1', 'tmdb-2', 'tmdb-3']);
  assert.deepEqual(a['tmdb-1'], { fileId: 10, upload: true });
  assert.deepEqual(a['tmdb-2'], { fileId: 20, upload: false });
  assert.deepEqual(a['tmdb-3'], { fileId: null, upload: false });
  assert.deepEqual(uploadFileIds(a, files), [10]);
});

test('ambiguous duplicate stored file numbers do not get arbitrary suggestions', () => {
  assert.equal(prepareAssignments(episodes, [file(1, 1), file(2, 1)])['tmdb-1'].fileId, null);
});

test('manual reassignment preserves File and ProjectEpisode identities and avoids duplicate file selection', () => {
  const originals = JSON.stringify({ files, episodes });
  const a = assignFile(prepareAssignments(episodes, files), 'tmdb-3', files[0]);
  assert.deepEqual(a['tmdb-1'], { fileId: null, upload: false });
  assert.deepEqual(a['tmdb-3'], { fileId: 10, upload: true });
  assert.equal(JSON.stringify({ files, episodes }), originals);
});

test('changing to an unaccepted file or clearing selection disables uploads', () => {
  let a = assignFile({}, 'tmdb-1', files[1]);
  assert.equal(a['tmdb-1'].upload, false);
  assert.deepEqual(uploadFileIds(a, files), []);
  a = assignFile(a, 'tmdb-1', null);
  assert.deepEqual(a['tmdb-1'], { fileId: null, upload: false });
});

test('stale refresh cannot overwrite a manual selection, removal or upload toggle', () => {
  const edited = { 'tmdb-1': { fileId: null, upload: false }, 'tmdb-3': { fileId: 10, upload: false } };
  const result = prepareAssignments(episodes, files, edited);
  assert.deepEqual(result['tmdb-1'], edited['tmdb-1']);
  assert.deepEqual(result['tmdb-3'], edited['tmdb-3']);
  assert.deepEqual(uploadFileIds(result, files), []);
});

test('backend eligibility is rechecked even for stale checked assignments', () => {
  const a = { one: { fileId: 20, upload: true }, two: { fileId: 10, upload: true } };
  assert.deepEqual(uploadFileIds(a, files), [10]);
  assert.deepEqual(uploadFileIds(a, [{ ...files[0], render_error: 'No subtitles' }]), []);
});

test('no-file metadata-only publish has empty upload selection', () => {
  assert.deepEqual(uploadFileIds(prepareAssignments(episodes, []), []), []);
});

test('stable episode keys use TMDB identity and project identity for movie content', () => {
  assert.equal(episodeKey(episodes[0]), 'tmdb-1');
  assert.equal(episodeKey({ id: 9, tmdb_episode_id: null, episode_number: 1, name_english: 'Movie' }), 'project:9');
});

const result: WebResult = { stage: 'Done', error: null, metadata: [], files: [], poster: null,
  operations: [{ stage: 'Creating title', outcome: 'created' }, { stage: 'Synchronizing episodes', outcome: 'noop' },
    { stage: 'Uploading episode 01', outcome: 'failed', error: 'Network failure' }, { stage: 'Poster', outcome: 'skipped' }] };
test('shared logs retain actual success, no-op, failure and skipped outcomes after completion', () => {
  const rows = publicationLines(result, false);
  assert.deepEqual(rows, result.operations);
  assert.deepEqual(rows.map(r => operationStatus(r.outcome)), ['OK', 'NOOP', 'FAILED', 'SKIPPED']);
});

test('running job events append accessible current operation without simulating work', () => {
  assert.equal(publicationLines(null, true, 'Uploading poster')[0].stage, 'Uploading poster');
  assert.equal(publicationLines(result, true, 'Updating progress').at(-1)?.outcome, 'running');
  const running = { ...result, operations: [{ stage: 'Poster', outcome: 'running' }] };
  assert.equal(publicationLines(running, true).length, 1);
});

test('legacy persisted results and top-level partial failures remain visible', () => {
  const legacy = { ...result, operations: undefined, metadata: [{ stage: 'Title', outcome: 'updated' }],
    poster: { outcome: 'noop' }, files: [{ file_id: 1, filename: '1.mkv', outcome: 'created' }],
    error: { code: 'network', message: 'Progress update failed' } };
  assert.deepEqual(publicationLines(legacy, false).map(r => r.outcome), ['updated', 'noop', 'created', 'failed']);
});

test('overall job progress stays monotonic within one attempt and resets for an explicit retry', () => {
  let p = advanceWorkProgress({ jobId: null, value: 0 }, 1, 25);
  p = advanceWorkProgress(p, 1, 5);
  assert.equal(p.value, 25);
  p = advanceWorkProgress(p, 1, 75);
  assert.equal(p.value, 75);
  assert.equal(advanceWorkProgress(p, 2, 0).value, 0);
  assert.equal(advanceWorkProgress(p, 1, 150).value, 100);
  assert.equal(advanceWorkProgress(p, 1, NaN).value, 75);
});
