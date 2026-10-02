import { useQuery } from '@tanstack/react-query';
import client from '../api/client';

export interface FileMetrics {
  file_id: number;
  filename: string;
  episode_number: number | null;
  prompt_version: string | null;
  events_total: number;
  events_user_edited: number;
  events_approved: number;
  edit_distance_norm: number | null;
  polish_churn_norm: number | null;
  polish_edit_count: number;
  qa_blockers: number;
  qa_warnings: number;
  qa_info: number;
  mean_confidence: number | null;
  mean_confidence_edited: number | null;
  llm_cost_usd: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
}

export function useProjectMetrics(projectId: number, enabled: boolean) {
  return useQuery<FileMetrics[]>({
    queryKey: ['projects', projectId, 'metrics'],
    queryFn: async () => {
      const { data } = await client.get<FileMetrics[]>(`/projects/${projectId}/metrics`);
      return data;
    },
    enabled,
    staleTime: 30_000,
    // Metrics refresh while files are still processing, so poll while open.
    refetchInterval: enabled ? 15_000 : false,
  });
}
