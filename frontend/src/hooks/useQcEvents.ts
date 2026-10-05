import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { isAxiosError } from 'axios';
import client from '../api/client';
import type { QcEventDetail, QcEventList } from '../types/qc';

// Deliberately NOT under the ['projects', ...] prefix: the many broad
// invalidateQueries({ queryKey: ['projects'] }) calls (live project_updated
// events, mutations) would otherwise refetch the multi-MB event list. QC data is
// only refetched explicitly (see useQcStale / the page's Reload action).
export const qcKey = (projectId: number, fileId: number) => ['qc', projectId, fileId] as const;

// Leaving the page drops the file's QC data after this grace period (the font
// bytes and the event list are large). Long enough to survive StrictMode's
// mount/unmount/mount and a quick back-and-forth.
export const QC_GC_TIME_MS = 5_000;

export type QcLoadErrorKind = 'not_found' | 'unavailable' | 'error';

/** 404 = unknown project/file, 409 = file exists but Final QC is not available. */
export function qcErrorKind(error: unknown): QcLoadErrorKind {
  if (isAxiosError(error)) {
    if (error.response?.status === 404) return 'not_found';
    if (error.response?.status === 409) return 'unavailable';
  }
  return 'error';
}

export function qcErrorDetail(error: unknown): string | null {
  if (isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string') return detail;
    return error.message;
  }
  return error instanceof Error ? error.message : null;
}

const retryTransient = (failureCount: number, error: unknown) =>
  qcErrorKind(error) === 'error' && failureCount < 2;

/** The whole file's (non-hidden unless asked) events, in backend order
 * (start_ms, line_index). Not paginated: ~4–5 MB for 10–15k events. */
export function useQcEvents(projectId: number, fileId: number, showHidden: boolean) {
  return useQuery<QcEventList>({
    queryKey: [...qcKey(projectId, fileId), 'events', { showHidden }],
    queryFn: async () => {
      const { data } = await client.get<QcEventList>(
        `/projects/${projectId}/files/${fileId}/qc/events`,
        { params: { show_hidden: showHidden } },
      );
      return data;
    },
    staleTime: Infinity,
    gcTime: QC_GC_TIME_MS,
    placeholderData: keepPreviousData,
    retry: retryTransient,
  });
}

/** Full QA/watched-word detail of ONE event, fetched lazily on selection. */
export function useQcEventDetail(projectId: number, fileId: number, eventId: number | null) {
  return useQuery<QcEventDetail>({
    queryKey: [...qcKey(projectId, fileId), 'event', eventId],
    queryFn: async () => {
      const { data } = await client.get<QcEventDetail>(
        `/projects/${projectId}/files/${fileId}/qc/events/${eventId}`,
      );
      return data;
    },
    enabled: eventId != null,
    staleTime: 60_000,
    gcTime: QC_GC_TIME_MS,
    placeholderData: keepPreviousData,
    retry: retryTransient,
  });
}
