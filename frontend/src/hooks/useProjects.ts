import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import type {
  ContextStatus,
  Project,
  ProjectStats,
  ProjectWatchedWord,
  SubtitleChunk,
  VideoFile,
  WatchedWordType,
} from '../types';

export function useProjects() {
  return useQuery<Project[]>({
    queryKey: ['projects'],
    queryFn: async () => {
      const { data } = await client.get<Project[]>('/projects');
      return data;
    },
  });
}

export function useProjectFiles(projectId: number | null) {
  return useQuery<VideoFile[]>({
    queryKey: ['projects', projectId, 'files'],
    queryFn: async () => {
      const { data } = await client.get<VideoFile[]>(`/projects/${projectId}/files`);
      return data;
    },
    enabled: projectId !== null,
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (projectId: number) => {
      await client.delete(`/projects/${projectId}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['import', 'directories'] });
    },
  });
}

export function usePauseProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (projectId: number) => {
      const { data } = await client.post<Project>(`/projects/${projectId}/pause`);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
  });
}

export function useResumeProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (projectId: number) => {
      const { data } = await client.post<Project>(`/projects/${projectId}/resume`);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
  });
}

export function useFileChunks(projectId: number, fileId: number, enabled: boolean) {
  return useQuery<SubtitleChunk[]>({
    queryKey: ['projects', projectId, 'files', fileId, 'chunks'],
    queryFn: async () => {
      const { data } = await client.get<SubtitleChunk[]>(`/projects/${projectId}/files/${fileId}/chunks`);
      return data;
    },
    enabled,
    staleTime: 10_000,
  });
}

export function useProjectStats(projectId: number | null) {
  return useQuery<ProjectStats>({
    queryKey: ['projects', projectId, 'stats'],
    queryFn: async () => {
      const { data } = await client.get<ProjectStats>(`/projects/${projectId}/stats`);
      return data;
    },
    enabled: projectId !== null,
    staleTime: 30_000,
  });
}

export function useRetryChunk(projectId: number, fileId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (chunkIndex: number) => {
      const { data } = await client.post<SubtitleChunk>(
        `/projects/${projectId}/files/${fileId}/chunks/${chunkIndex}/retry`,
      );
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files', fileId, 'chunks'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files'] });
    },
  });
}

export function useAcceptFileReview(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ fileId, resolveWarnings = false }: { fileId: number; resolveWarnings?: boolean }) => {
      const { data } = await client.post<VideoFile>(
        `/projects/${projectId}/files/${fileId}/accept-review`,
        { resolve_warnings: resolveWarnings },
      );
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<VideoFile[]>(
        ['projects', projectId, 'files'],
        (files) => files?.map((file) => (file.id === data.id ? { ...file, ...data } : file)),
      );
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files', data.id, 'chunks'] });
    },
  });
}

export function useContextStatus(projectId: number | null, enabled = true) {
  return useQuery<ContextStatus>({
    queryKey: ['projects', projectId, 'context'],
    queryFn: async () => {
      const { data } = await client.get<ContextStatus>(`/projects/${projectId}/context-status`);
      return data;
    },
    enabled: enabled && projectId !== null,
    staleTime: 5_000,
  });
}

export function useApproveContext(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await client.post<Project>(`/projects/${projectId}/approve-context`);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useRetryContextComponent(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (component: 'speaker_aggregation' | 'character_mapping' | 'style_bible') => {
      const { data } = await client.post(`/projects/${projectId}/context/retry`, { component });
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'context'] });
    },
  });
}

export function useTranslateFile(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (fileId: number) => {
      const { data } = await client.post<VideoFile>(`/projects/${projectId}/files/${fileId}/translate`);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<VideoFile[]>(
        ['projects', projectId, 'files'],
        (files) => files?.map((file) => (file.id === data.id ? { ...file, ...data } : file)),
      );
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useRetranslateFile(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (fileId: number) => {
      const { data } = await client.post<VideoFile>(`/projects/${projectId}/files/${fileId}/retranslate`);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<VideoFile[]>(
        ['projects', projectId, 'files'],
        (files) => files?.map((file) => (file.id === data.id ? { ...file, ...data } : file)),
      );
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'files', data.id, 'chunks'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'metrics'] });
    },
  });
}

export function useProjectWatchedWords(projectId: number | null, enabled = true) {
  return useQuery<ProjectWatchedWord[]>({
    queryKey: ['projects', projectId, 'watched-words'],
    queryFn: async () => {
      const { data } = await client.get<ProjectWatchedWord[]>(`/projects/${projectId}/watched-words`);
      return data;
    },
    enabled: enabled && projectId !== null,
    staleTime: 30_000,
  });
}

export function useCreateProjectWatchedWord(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { word: string; word_type: WatchedWordType }) => {
      const { data } = await client.post<ProjectWatchedWord>(`/projects/${projectId}/watched-words`, payload);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<ProjectWatchedWord[]>(
        ['projects', projectId, 'watched-words'],
        (words) => {
          const next = [...(words ?? []), data];
          return next.sort((a, b) => (
            a.word_type === b.word_type
              ? a.word.localeCompare(b.word)
              : a.word_type.localeCompare(b.word_type)
          ));
        },
      );
    },
  });
}

export function useDeleteProjectWatchedWord(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (watchedWordId: number) => {
      await client.delete(`/projects/${projectId}/watched-words/${watchedWordId}`);
      return watchedWordId;
    },
    onSuccess: (watchedWordId) => {
      queryClient.setQueryData<ProjectWatchedWord[]>(
        ['projects', projectId, 'watched-words'],
        (words) => words?.filter((word) => word.id !== watchedWordId),
      );
    },
  });
}

/** Invalidate project + file queries when a job completes — call inside useJobSocket. */
export function useInvalidateProjectsOnJob() {
  // No-op — invalidation is now handled inside useJobSocket directly.
}
