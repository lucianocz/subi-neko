import { useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import type { QcBranding, QcBrandingSave } from '../types/qc';
import { QC_GC_TIME_MS, qcKey } from './useQcEvents';

const brandingKey = (projectId: number, fileId: number) => [...qcKey(projectId, fileId), 'branding'] as const;

/** Branding config + the template list (both small; loaded with the QC page). */
export function useQcBranding(projectId: number, fileId: number) {
  const queryClient = useQueryClient();
  const base = `/projects/${projectId}/files/${fileId}/qc/branding`;

  const config = useQuery<QcBranding>({
    queryKey: brandingKey(projectId, fileId),
    queryFn: async () => (await client.get<QcBranding>(base)).data,
    staleTime: Infinity,
    gcTime: QC_GC_TIME_MS,
    retry: 1,
  });
  const templates = useQuery<string[]>({
    queryKey: [...brandingKey(projectId, fileId), 'templates'],
    queryFn: async () => (await client.get<{ templates: string[] }>(`${base}/templates`)).data.templates,
    // The directory can change between dialog opens.
    staleTime: 0,
    gcTime: QC_GC_TIME_MS,
    retry: 1,
  });

  /** Persist; resolves with the authoritative saved config. Rejects with the axios error. */
  const save = useCallback(async (body: QcBrandingSave): Promise<QcBranding> => {
    const { data } = await client.put<QcBranding>(base, body);
    queryClient.setQueryData(brandingKey(projectId, fileId), data);
    return data;
  }, [base, queryClient, projectId, fileId]);

  return { config, templates, save };
}
