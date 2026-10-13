import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  apiErrorDetail,
  apiErrorMessage,
  archiveMenuState,
  compareArchived,
  directoryName,
  filterArchivedProjects,
  invalidationKeys,
  normalizeSearchText,
  publishDisabledReason,
  secondaryTitle,
  type ArchivedProjectRow,
} from '../src/utils/projectArchive.ts';

const row = (
  id: number,
  name: string,
  archived_at: string,
  extra: Partial<ArchivedProjectRow> = {},
): ArchivedProjectRow => ({
  id, name, archived_at, name_english: null, source_directory: `dir-${id}`, file_count: 1, ...extra,
});

// ─── Titles ──────────────────────────────────────────────────────────────────

test('secondary title: shown only for a genuine, different English title', () => {
  assert.equal(secondaryTitle({ name: 'Sousou no Frieren', name_english: "Frieren: Beyond Journey's End" }),
    "Frieren: Beyond Journey's End");
  assert.equal(secondaryTitle({ name: 'Frieren', name_english: null }), null);
  assert.equal(secondaryTitle({ name: 'Frieren' }), null);
  assert.equal(secondaryTitle({ name: 'Frieren', name_english: '   ' }), null);
  assert.equal(secondaryTitle({ name: 'Bleach', name_english: ' bleach ' }), null);   // same title in other case
  assert.equal(secondaryTitle({ name: 'A', name_english: '  B  ' }), 'B');            // trimmed
});

test('directoryName takes the last segment with either separator', () => {
  assert.equal(directoryName('Series/Frieren'), 'Frieren');
  assert.equal(directoryName('Series\\Frieren\\'), 'Frieren');
  assert.equal(directoryName('Frieren'), 'Frieren');
});

// ─── Archived list: ordering and search ──────────────────────────────────────

test('archived rows sort by archive date descending with a deterministic tie-break', () => {
  const rows = [
    row(1, 'a', '2026-01-01T00:00:00'),
    row(2, 'b', '2026-03-01T00:00:00'),
    row(3, 'c', '2026-03-01T00:00:00'),
    row(4, 'd', '2026-02-01T00:00:00'),
  ];
  assert.deepEqual(filterArchivedProjects(rows, '').map((r) => r.id), [3, 2, 4, 1]);
  assert.deepEqual([...rows].reverse().sort(compareArchived).map((r) => r.id), [3, 2, 4, 1]);
});

test('search is case- and accent-insensitive across title, English title and folder', () => {
  const rows = [
    row(1, 'Šíleně smutná', '2026-01-01', { name_english: 'Terribly Sad', source_directory: 'Anime/Sad Series' }),
    row(2, 'Sousou no Frieren', '2026-02-01', { name_english: "Frieren: Beyond Journey's End", source_directory: 'Frieren S01' }),
    row(3, 'Bleach', '2026-03-01', { source_directory: 'Přehlídka/bleach_tybw' }),
  ];
  const ids = (q: string) => filterArchivedProjects(rows, q).map((r) => r.id);

  assert.deepEqual(ids('sileně'), [1]);            // accent-insensitive: "Šíleně" ← "sileně"
  assert.deepEqual(ids('SILENE SMUTNA'), [1]);     // case + accents, multiple terms
  assert.deepEqual(ids('terribly'), [1]);          // English title
  assert.deepEqual(ids('journey'), [2]);
  assert.deepEqual(ids('s01'), [2]);               // folder
  assert.deepEqual(ids('prehlidka'), [3]);         // accented folder, plain query
  assert.deepEqual(ids('tybw'), [3]);
  assert.deepEqual(ids('frieren bleach'), []);     // every term must match one row
  assert.deepEqual(ids('nothing like this'), []);
  assert.deepEqual(ids('   '), [3, 2, 1]);         // blank = everything, date-descending
});

test('filtering keeps the date-descending order and does not mutate the cache', () => {
  const cache = Object.freeze([
    row(1, 'Alpha one', '2026-01-01'),
    row(2, 'Alpha two', '2026-05-01'),
    row(3, 'Beta', '2026-03-01'),
  ]);
  assert.deepEqual(filterArchivedProjects(cache, 'alpha').map((r) => r.id), [2, 1]);
  assert.deepEqual(cache.map((r) => r.id), [1, 2, 3]);
});

test('search over hundreds of archived projects stays fast', () => {
  const rows = Array.from({ length: 2000 }, (_, i) =>
    row(i + 1, `Série číslo ${i}`, `2026-01-${String((i % 28) + 1).padStart(2, '0')}`, {
      name_english: `Series number ${i}`, source_directory: `Anime/Folder ${i}`,
    }));
  const start = performance.now();
  for (let i = 0; i < 20; i += 1) filterArchivedProjects(rows, 'serie cislo 19');
  assert.ok(performance.now() - start < 2000);
  assert.ok(filterArchivedProjects(rows, 'serie cislo 1999').length === 1);
});

test('normalizeSearchText folds case and combining marks', () => {
  assert.equal(normalizeSearchText('ŽLUŤOUČKÝ Kůň'), 'zlutoucky kun');
});

// ─── Archive action ──────────────────────────────────────────────────────────

test('archive menu item: disabled with the backend reason, allowed otherwise or when unknown', () => {
  assert.deepEqual(archiveMenuState({ archive: { eligible: true, reason: null } }), { disabled: false, reason: null });
  assert.deepEqual(
    archiveMenuState({ archive: { eligible: false, reason: '1 of 3 file(s) are not accepted yet.' } }),
    { disabled: true, reason: '1 of 3 file(s) are not accepted yet.' },
  );
  assert.equal(archiveMenuState({ archive: { eligible: false, reason: null } }).disabled, true);
  assert.deepEqual(archiveMenuState({}), { disabled: false, reason: null });   // backend re-checks anyway
});

test('archive, unarchive and delete refresh both lists; delete also the import directories', () => {
  const flat = (k: Parameters<typeof invalidationKeys>[0]) => invalidationKeys(k).map((key) => key.join('/'));
  assert.deepEqual(flat('archive'), ['projects', 'archived-projects']);
  assert.deepEqual(flat('unarchive'), ['projects', 'archived-projects']);
  assert.deepEqual(flat('delete'), ['projects', 'archived-projects', 'import/directories']);
});

test('API error details: plain string, {code, message}, and anything else', () => {
  const err = (detail: unknown) => ({ response: { data: { detail } } });
  assert.deepEqual(apiErrorDetail(err('boom')), { code: null, message: 'boom' });
  assert.deepEqual(
    apiErrorDetail(err({ code: 'project_archived', message: 'This project is archived.' })),
    { code: 'project_archived', message: 'This project is archived.' },
  );
  assert.deepEqual(apiErrorDetail(err([{ loc: [] }])), { code: null, message: null });
  assert.deepEqual(apiErrorDetail(new Error('network')), { code: null, message: null });
  assert.equal(apiErrorMessage(new Error('network'), 'fallback'), 'fallback');
});

// ─── Publish availability ────────────────────────────────────────────────────

test('publish is disabled for archived projects and failed preflight, with a reason', () => {
  const ok = { available: true, reason: null, missing_files: [] };
  assert.equal(publishDisabledReason({ archived_at: null }, ok), null);
  assert.equal(publishDisabledReason({ archived_at: null }, undefined), null);   // not loaded: backend decides
  assert.match(publishDisabledReason({ archived_at: '2026-01-01' }, ok) ?? '', /archived/i);
  assert.equal(
    publishDisabledReason({}, { available: false, reason: 'The source folder is missing.', missing_files: [] }),
    'The source folder is missing.',
  );
  assert.ok(publishDisabledReason({}, { available: false, reason: null, missing_files: ['a.mkv'] }));
});

// ─── Wiring (no DOM available: structural checks on the sources) ─────────────

const src = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('sidebar card shows both titles and never the source folder', () => {
  const layout = src('../src/components/Layout/AppLayout.tsx');
  const card = layout.slice(layout.indexOf('function ProjectCard'), layout.indexOf('function ProjectSidebar'));
  assert.ok(card.includes('project.name'));
  assert.ok(card.includes('secondaryTitle(project)'));
  assert.ok(!card.includes('source_directory'));
});

test('Archived projects link sits right below the list, not pinned to the bottom', () => {
  const layout = src('../src/components/Layout/AppLayout.tsx');
  const sidebar = layout.slice(layout.indexOf('function ProjectSidebar'), layout.indexOf('function EmptyProjectLanding'));
  const scroll = sidebar.indexOf('</ScrollArea>');
  const link = sidebar.indexOf('to="/archived"');
  assert.ok(scroll > 0 && link > scroll);
  assert.ok(sidebar.indexOf('<ActiveJobsPanel') > link);                 // jobs panel stays below
  assert.ok(sidebar.includes("flex: '0 1 auto'"));                        // list shrinks/scrolls, link stays visible
  assert.ok(sidebar.includes('active={archivedActive}'));
});

test('header kebab menu: Archive first, then refresh / pause, Remove last and red; other buttons stay', () => {
  const layout = src('../src/components/Layout/AppLayout.tsx');
  const header = layout.slice(layout.indexOf('{/* Project header */}'), layout.indexOf('{/* Pipeline */}'));
  const order = ['Archive project', 'Refresh metadata', "'Resume' : 'Pause'", 'Remove project'].map((t) => header.indexOf(t));
  assert.ok(order.every((i) => i > 0) && [...order].sort((a, b) => a - b).join() === order.join());
  assert.ok(header.includes('<Menu.Divider />'));
  assert.ok(header.indexOf('Menu.Divider') < header.indexOf('Remove project'));
  for (const label of ['Metrics', 'Review queue', 'Style guide', 'Watched words', 'Styles &amp; fonts']) {
    assert.ok(header.includes(label), `${label} stays in the header`);
  }
  // the moved actions are no longer standalone header buttons
  assert.ok(!/<Button[^>]*>\s*Refresh metadata\s*<\/Button>/.test(header));
});

test('archive requires confirmation and leaves the archived detail page afterwards', () => {
  const layout = src('../src/components/Layout/AppLayout.tsx');
  assert.ok(layout.includes('all data is preserved') || layout.includes('All data is preserved'));
  assert.ok(layout.includes('disappear from the active project list'));
  assert.match(layout, /onArchived=\{\(\) => navigate\('\/'\)\}/);
});

test('direct archived-project URL shows the informational state', () => {
  const layout = src('../src/components/Layout/AppLayout.tsx');
  assert.ok(layout.includes('This project is archived'));
  assert.ok(layout.includes('Unarchive this project to access its files, translations and editing tools.'));
  assert.ok(layout.includes('Unarchive project'));
  assert.ok(layout.includes("navigate('/archived')"));
  const app = src('../src/App.tsx');
  assert.ok(app.includes('path="/archived"') && app.includes('path="/projects/:projectId"'));
});

test('archived page: Unarchive and Delete menu, shared delete confirmation, no duplicate deletion logic', () => {
  const page = src('../src/pages/ArchivedProjectsPage.tsx');
  assert.ok(page.includes('Unarchive') && page.includes('Delete'));
  assert.ok(page.includes('RemoveProjectModal'));
  assert.ok(!page.includes('useDeleteProject'));
  assert.ok(page.includes('Open project'));                              // navigation to the restored project
  const modal = src('../src/components/Project/RemoveProjectModal.tsx');
  assert.ok(modal.includes('Files on disk will not be touched.'));
});

test('publish: preflight gates the button and is re-checked before every attempt', () => {
  const pipeline = src('../src/components/Project/ProjectPipeline.tsx');
  assert.ok(pipeline.includes('usePublishPreflight'));
  assert.ok(pipeline.includes('await preflight.refetch()'));
  assert.ok(pipeline.includes('disabled={publishBlocked !== null}'));
  const hooks = src('../src/hooks/useProjects.ts');
  assert.ok(hooks.includes("queryKey: ['publish-preflight'"));            // outside ['projects']: no re-scan per websocket event
});
