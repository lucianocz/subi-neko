import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';

export interface ProjectStyle {
  id: number;
  project_id: number;
  style_name: string;
  font_name: string;
  font_size: number;
  replacement_font_name: string | null;
  replacement_font_size: number | null;
  font_check_status: string;
  file_count: number;
}

export interface ProjectStyleUpdate {
  replacement_font_name: string | null;
  replacement_font_size: number | null;
}

const key = (projectId: number) => ['projects', projectId, 'styles'] as const;

export function useProjectStyles(projectId: number, enabled: boolean) {
  return useQuery<ProjectStyle[]>({
    queryKey: key(projectId),
    queryFn: async () => {
      const { data } = await client.get<ProjectStyle[]>(`/projects/${projectId}/styles`);
      return data;
    },
    enabled,
  });
}

/** One request updates the canonical style, and with it every file sharing it. */
export function useUpdateProjectStyle(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ styleId, ...body }: ProjectStyleUpdate & { styleId: number }) => {
      const { data } = await client.put<ProjectStyle>(`/projects/${projectId}/styles/${styleId}`, body);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<ProjectStyle[]>(
        key(projectId),
        (styles) => styles?.map((s) => (s.id === data.id ? data : s)),
      );
    },
  });
}
