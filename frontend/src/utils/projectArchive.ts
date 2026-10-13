// Pure view-model helpers for archiving, bilingual titles and publish
// availability — kept free of React so they can be unit-tested with node:test.

export interface TitledProject {
  name: string;
  name_english?: string | null;
}

/** English title shown under the romanized one, or null when there is no
 * genuine second title (missing, blank, or the same text as the primary). */
export function secondaryTitle(project: TitledProject): string | null {
  const english = project.name_english?.trim();
  if (!english) return null;
  return english.toLocaleLowerCase() === project.name.trim().toLocaleLowerCase() ? null : english;
}

/** Last path segment of a source directory (either separator). */
export function directoryName(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  return normalized.split('/').pop() || path;
}

// ─── Archived projects page ──────────────────────────────────────────────────

export interface ArchivedProjectRow extends TitledProject {
  id: number;
  source_directory: string;
  archived_at: string;
  file_count: number;
}

/** Case- and accent-insensitive normal form used for searching. */
export function normalizeSearchText(text: string): string {
  return text
    .normalize('NFD')
    .replace(/\p{M}+/gu, '')
    .toLocaleLowerCase();
}

/** Newest archive first; id descending breaks ties so the order is stable. */
export function compareArchived(
  a: Pick<ArchivedProjectRow, 'id' | 'archived_at'>,
  b: Pick<ArchivedProjectRow, 'id' | 'archived_at'>,
): number {
  if (a.archived_at !== b.archived_at) return a.archived_at < b.archived_at ? 1 : -1;
  return b.id - a.id;
}

/** Client-side filter over every loaded row (title, English title, source
 * folder). All whitespace-separated terms must match; order stays archive-date
 * descending whatever the filter. */
export function filterArchivedProjects<T extends ArchivedProjectRow>(
  rows: readonly T[],
  query: string,
): T[] {
  const terms = normalizeSearchText(query).split(/\s+/).filter(Boolean);
  const sorted = [...rows].sort(compareArchived);
  if (terms.length === 0) return sorted;
  return sorted.filter((row) => {
    const haystack = normalizeSearchText(
      `${row.name}\n${row.name_english ?? ''}\n${row.source_directory}`,
    );
    return terms.every((term) => haystack.includes(term));
  });
}

// ─── Archive action ──────────────────────────────────────────────────────────

export interface ArchiveInfo {
  eligible: boolean;
  reason: string | null;
}

/** State of the header menu's "Archive project" item. The backend re-checks on
 * the action, so an unknown state is simply allowed. */
export function archiveMenuState(project: { archive?: ArchiveInfo | null }): {
  disabled: boolean;
  reason: string | null;
} {
  const info = project.archive;
  if (!info || info.eligible) return { disabled: false, reason: null };
  return { disabled: true, reason: info.reason ?? 'This project cannot be archived right now.' };
}

/** Query-key prefixes to invalidate after each archive-related mutation. */
export type ArchiveMutation = 'archive' | 'unarchive' | 'delete';

export function invalidationKeys(kind: ArchiveMutation): (readonly unknown[])[] {
  const common: (readonly unknown[])[] = [['projects'], ['archived-projects']];
  return kind === 'delete' ? [...common, ['import', 'directories']] : common;
}

// ─── API errors ──────────────────────────────────────────────────────────────

interface ErrorLike {
  response?: { data?: { detail?: unknown } };
}

/** FastAPI error detail: a string, or {code, message}. */
export function apiErrorDetail(err: unknown): { code: string | null; message: string | null } {
  const detail = (err as ErrorLike | null)?.response?.data?.detail;
  if (typeof detail === 'string') return { code: null, message: detail };
  if (detail && typeof detail === 'object') {
    const { code, message } = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof code === 'string' ? code : null,
      message: typeof message === 'string' ? message : null,
    };
  }
  return { code: null, message: null };
}

export function apiErrorMessage(err: unknown, fallback: string): string {
  return apiErrorDetail(err).message ?? fallback;
}

// ─── Publish availability ────────────────────────────────────────────────────

export interface PublishPreflightInfo {
  available: boolean;
  reason: string | null;
  missing_files: string[];
}

/** Why the Publish button should be disabled, or null when it may be used.
 * `archived` wins over the backend preflight (which says the same thing but
 * may not have loaded yet); an unloaded preflight never blocks — the backend
 * re-validates the request. */
export function publishDisabledReason(
  project: { archived_at?: string | null },
  preflight: PublishPreflightInfo | undefined,
): string | null {
  if (project.archived_at) return 'The project is archived. Unarchive it to publish.';
  if (preflight && !preflight.available) {
    return preflight.reason ?? 'Publishing is not available right now.';
  }
  return null;
}
