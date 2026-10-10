import test from 'node:test';
import assert from 'node:assert/strict';
import { QueryClient } from '@tanstack/react-query';
import { invalidateMappingQueries } from '../src/utils/mappingInvalidation.ts';

const KEYS = ['characters', 'speakers', 'context', 'files'] as const;

function seed(qc: QueryClient, pid: number) {
  for (const k of KEYS) qc.setQueryData(['projects', pid, k], { k });
}
const stale = (qc: QueryClient, pid: number, k: string) =>
  qc.getQueryState(['projects', pid, k])!.isInvalidated;

test('mapping invalidation marks characters, speakers and context stale for that project only', async () => {
  const qc = new QueryClient();
  seed(qc, 1);
  seed(qc, 2);
  await invalidateMappingQueries(qc, 1);
  assert.equal(stale(qc, 1, 'characters'), true);
  assert.equal(stale(qc, 1, 'speakers'), true);
  assert.equal(stale(qc, 1, 'context'), true);
  assert.equal(stale(qc, 1, 'files'), false); // job/file state untouched
  for (const k of KEYS) assert.equal(stale(qc, 2, k), false); // other project untouched
});

test('active context query is refetched in place, keeping its data (no loading flicker)', async () => {
  const qc = new QueryClient();
  let calls = 0;
  const seen: string[] = [];
  const obs = qc.getQueryCache().build(qc, {
    queryKey: ['projects', 1, 'context'],
    queryFn: async () => ({ unmapped: ++calls }),
  });
  qc.setQueryData(['projects', 1, 'context'], { unmapped: 0 });
  const { QueryObserver } = await import('@tanstack/react-query');
  const o = new QueryObserver(qc, {
    queryKey: ['projects', 1, 'context'],
    queryFn: async () => ({ unmapped: ++calls }),
    staleTime: 60_000,
  });
  const unsub = o.subscribe((r) => seen.push(`${r.status}:${r.isLoading}`));
  await invalidateMappingQueries(qc, 1);
  unsub();
  assert.equal(obs.state.data !== undefined, true);
  assert.deepEqual((qc.getQueryData(['projects', 1, 'context']) as { unmapped: number }).unmapped, calls);
  assert.ok(!seen.some((s) => s.endsWith(':true')), 'never reverts to loading');
});

test('quick consecutive invalidations: only the latest response is kept', async () => {
  const qc = new QueryClient();
  const { QueryObserver } = await import('@tanstack/react-query');
  let n = 0;
  const o = new QueryObserver(qc, {
    queryKey: ['projects', 1, 'context'],
    queryFn: async ({ signal }) => {
      const mine = ++n;
      await new Promise((r) => setTimeout(r, mine === 2 ? 5 : 40));
      if (signal.aborted) throw new Error('aborted');
      return { v: mine };
    },
    staleTime: 60_000,
  });
  const unsub = o.subscribe(() => {});
  await new Promise((r) => setTimeout(r, 60)); // initial fetch (#1) done
  const p1 = invalidateMappingQueries(qc, 1); // refetch #2 starts
  const p2 = invalidateMappingQueries(qc, 1); // cancels #2, starts #3
  await Promise.all([p1, p2]);
  unsub();
  assert.equal((qc.getQueryData(['projects', 1, 'context']) as { v: number }).v, n);
});

test('a failed mutation never reaches onSuccess, so nothing is invalidated', async () => {
  const qc = new QueryClient();
  seed(qc, 1);
  const { MutationObserver } = await import('@tanstack/react-query');
  const m = new MutationObserver(qc, {
    mutationFn: async () => { throw new Error('409'); },
    onSuccess: () => { void invalidateMappingQueries(qc, 1); },
  });
  await assert.rejects(m.mutate());
  for (const k of KEYS) assert.equal(stale(qc, 1, k), false);
});
