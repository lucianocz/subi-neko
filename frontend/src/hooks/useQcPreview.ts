import { useQuery } from '@tanstack/react-query';
import client from '../api/client';
import type { QcPreview } from '../types/qc';
import { QC_GC_TIME_MS, qcKey } from './useQcEvents';

export function qcPreviewUrl(projectId: number, fileId: number) {
  return `/projects/${projectId}/files/${fileId}/qc/preview.ass`;
}

export const qcPreviewKey = (projectId: number, fileId: number) => [...qcKey(projectId, fileId), 'preview'] as const;

/** Authoritative translated ASS (the backend's `build_ass`; never generated in TS). */
export async function fetchQcPreview(projectId: number, fileId: number): Promise<QcPreview> {
  const res = await client.get<string>(qcPreviewUrl(projectId, fileId), {
    responseType: 'text',
    transformResponse: (data) => data,
  });
  return { text: res.data, revision: Number(res.headers['x-output-revision'] ?? 0) };
}

/** Initial load only: later refreshes go through `fetchQcPreview` + `setTrack`. */
export function useQcPreview(projectId: number, fileId: number) {
  return useQuery<QcPreview>({
    queryKey: qcPreviewKey(projectId, fileId),
    queryFn: () => fetchQcPreview(projectId, fileId),
    staleTime: Infinity,
    gcTime: QC_GC_TIME_MS,
    retry: 1,
  });
}
