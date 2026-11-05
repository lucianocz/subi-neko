import type { WebFile, WebMapping } from './webPublishing.ts';
import type { WebResult } from '../hooks/useWebPublishing.ts';

export interface PublishEpisode extends WebMapping { id?: number }
export const episodeKey = (e: PublishEpisode): string => e.tmdb_episode_id ?? `project:${e.id ?? e.episode_number}`;
export interface EpisodeAssignment { fileId: number | null; upload: boolean }
export type Assignments = Record<string, EpisodeAssignment>;

/** Initial suggestions only. Existing user decisions (including no file) win. */
export function prepareAssignments(episodes: PublishEpisode[], files: WebFile[], current: Assignments = {}): Assignments {
  const next = { ...current };
  const used = new Set(Object.values(current).flatMap(a => a.fileId === null ? [] : [a.fileId]));
  for (const episode of episodes) {
    const key = episodeKey(episode);
    if (Object.hasOwn(next, key)) continue;
    const matches = files.filter(f => !used.has(f.id) && f.episode_number === episode.episode_number);
    const file = matches.length === 1 ? matches[0] : null;
    next[key] = { fileId: file?.id ?? null, upload: file?.status === 'accepted' && !file.render_error };
    if (file) used.add(file.id);
  }
  return next;
}

export function assignFile(current: Assignments, key: string, file: WebFile | null): Assignments {
  const next = { ...current };
  // A file is selected for exactly one stable project episode.
  for (const [other, assignment] of Object.entries(next)) {
    if (other !== key && file && assignment.fileId === file.id) next[other] = { fileId: null, upload: false };
  }
  next[key] = { fileId: file?.id ?? null, upload: file?.status === 'accepted' && !file.render_error };
  return next;
}

export function uploadFileIds(assignments: Assignments, files: WebFile[]): number[] {
  return [...new Set(Object.values(assignments).filter(a => a.upload && files.some(f => f.id === a.fileId && f.status === 'accepted' && !f.render_error))
    .flatMap(a => a.fileId === null ? [] : [a.fileId]))];
}

export function publicationLines(result: WebResult | null | undefined, running: boolean, message?: string | null): { stage: string; outcome: string; error?: string }[] {
  const rows: { stage: string; outcome: string; error?: string }[] = result?.operations ? [...result.operations] : [
    ...(result?.metadata ?? []),
    ...(result?.poster ? [{ stage: 'Uploading poster', ...result.poster }] : []),
    ...(result?.files ?? []).map(f => ({ stage: `Uploading ${f.filename}`, outcome: f.outcome, error: f.error })),
  ];
  if (running && !rows.some(r => r.outcome === 'running')) rows.push({ stage: message || result?.stage || 'Queued', outcome: 'running' });
  if (result?.error && !rows.some(r => r.outcome === 'failed' && r.error === result.error?.message)) rows.push({ stage: result.stage, outcome: 'failed', error: result.error.message });
  return rows;
}

export const operationStatus = (outcome: string): string => ({ verified: 'OK', found: 'OK', not_found: 'NOOP', uncertain: 'FAILED', running: 'RUNNING', pending: 'WAITING', created: 'OK', updated: 'OK', completed: 'OK', noop: 'NOOP', failed: 'FAILED', skipped: 'SKIPPED', unavailable: 'SKIPPED' })[outcome] ?? outcome.toUpperCase();

export interface WorkProgress { jobId: number | null; value: number }
/** Ignore stale/lower samples within one attempt, reset for an explicit retry. */
export function advanceWorkProgress(before: WorkProgress, jobId: number | null, sample: number): WorkProgress {
  const value = Math.max(0, Math.min(100, Number.isFinite(sample) ? sample : 0));
  return { jobId, value: before.jobId === jobId ? Math.max(before.value, value) : value };
}
