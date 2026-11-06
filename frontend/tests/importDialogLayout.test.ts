import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { IMPORT_MODAL_WIDTH } from '../src/utils/tmdbImport.ts';
const read = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8');
const dialog = read('../src/pages/ImportDialog.tsx');
const editor = read('../src/components/Tmdb/TmdbMappingEditor.tsx');
const web = read('../src/components/Project/WebPublishDialog.tsx');
const progress = read('../src/components/Project/PublicationProgress.tsx');

test('source screen keeps wide directory/search layout and adds independent import', () => {
  assert.equal(IMPORT_MODAL_WIDTH, 'min(1240px, 96vw)');
  assert.match(dialog, /size=\{IMPORT_MODAL_WIDTH\}/);
  assert.match(dialog, /<SimpleGrid type="container" cols=\{RESULT_GRID_COLUMNS\}/);
  assert.match(dialog, /onClick=\{importSource\}>Import/);
  assert.match(dialog, /Publish settings/);
  assert.match(dialog, /function handleNext[\s\S]*?setStep\('mapping'\)/);
});

test('both dialogs use the same full editor and progress component', () => {
  assert.match(read('../src/components/Import/TmdbMappingStep.tsx'), /TmdbMappingEditor as TmdbMappingStep/);
  assert.match(web, /<TmdbMappingEditor publishMode .*locked=/);
  assert.match(dialog, /<PublicationProgress state=/);
  assert.match(web, /<PublicationProgress state=/);
  assert.equal((progress.match(/<Progress /g) ?? []).length, 1);
  assert.match(progress, /role="log"[\s\S]*?maxHeight: 300/);
});

test('the right column holds both metadata and compact placement controls', () => {
  const right = editor.slice(editor.indexOf('{/* Right:'));
  assert.match(right, /borderLeft: '1px solid/);
  assert.match(right, /<TitleMetadataFields compact/);
  for (const name of ['order', 'group', 'structure', 'season', 'expected']) assert.ok(right.includes(`w={FIELD_WIDTH.${name}}`));
  assert.doesNotMatch(editor, /maxWidth: 520|Import without TMDB mapping<|Import Only<|Publish &amp; Import</);
  assert.match(editor, /Save &amp; Publish/);
});

test('import and publication are separate and retries keep a stable local identity', () => {
  assert.match(dialog, /if \(submitGuard.current \|\| imported\) return/);
  assert.match(dialog, /request_id: request.current.id/);
  assert.match(dialog, /setImported\(created\); setStep\('progress'\)/);
  assert.match(dialog, /startPublication\(imported.id\)/);
  assert.match(dialog, /Project imported:/);
  assert.match(dialog, /publication.data\?\.state !== 'publishing'/);
});

test('WEB has three steps, automatic suggestions and per-episode upload eligibility', () => {
  assert.equal((web.match(/<Stepper.Step /g) ?? []).length, 3);
  assert.doesNotMatch(web, /Refresh episodes|Suggest from stored episode numbers/);
  assert.match(web, /prepareAssignments\(saved.episodes/);
  assert.match(web, /file\?\.status === 'accepted'/);
  assert.match(web, /disabled=\{!hasUploads \|\| pending\}/);
});

test('structural controls lock independently from editable descriptive metadata', () => {
  for (const label of ['Episode order', 'Structure', 'Season / subgroup']) assert.ok(editor.includes(`<Select disabled={locked} label="${label}"`));
  assert.match(editor, /<TextInput label="Search TMDB" disabled=\{locked\}/);
  assert.doesNotMatch(editor, /<NumberInput disabled=\{locked\} label="Total ep."/);
  assert.match(editor, /Refresh metadata/);
  assert.match(editor, /applyLatest\(selectGuard.current/);
  assert.match(editor, /applyLatest\(translateGuard.current/);
});
