import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';
import { invalidateMappingQueries } from '../utils/mappingInvalidation';
import type {
  ProjectCharacterWithSpeakers,
  ProjectSpeaker,
  SpeakerContentTag,
  SpeakerUpdateResult,
} from '../types';

interface CharacterUpdatePayload {
  projectId: number;
  characterId: number;
  gender?: string | null;
  social_position?: string | null;
  note?: string | null;
  speaker_ids?: number[];
}

interface SpeakerUpdatePayload {
  projectId: number;
  speakerId: number;
  gender?: string | null;
  character_id?: number | null;
  is_extra?: boolean;
  content_tag?: SpeakerContentTag | null;
}

export function useProjectCharacters(projectId: number | null) {
  return useQuery<ProjectCharacterWithSpeakers[]>({
    queryKey: ['projects', projectId, 'characters'],
    queryFn: async () => {
      const { data } = await client.get<ProjectCharacterWithSpeakers[]>(
        `/projects/${projectId}/characters`,
      );
      return data;
    },
    enabled: projectId !== null,
  });
}

export function useProjectSpeakers(projectId: number | null) {
  return useQuery<ProjectSpeaker[]>({
    queryKey: ['projects', projectId, 'speakers'],
    queryFn: async () => {
      const { data } = await client.get<ProjectSpeaker[]>(
        `/projects/${projectId}/speakers`,
      );
      return data;
    },
    enabled: projectId !== null,
  });
}

export function useUpdateCharacter() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: CharacterUpdatePayload) => {
      const { projectId, characterId, ...body } = payload;
      const { data } = await client.put<ProjectCharacterWithSpeakers>(
        `/projects/${projectId}/characters/${characterId}`,
        body,
      );
      return data;
    },
    onSuccess: (_data, variables) => {
      invalidateMappingQueries(queryClient, variables.projectId);
    },
  });
}

export function useUpdateSpeaker() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: SpeakerUpdatePayload) => {
      const { projectId, speakerId, ...body } = payload;
      const { data } = await client.put<SpeakerUpdateResult>(
        `/projects/${projectId}/speakers/${speakerId}`,
        body,
      );
      return data;
    },
    onSuccess: (_data, variables) => {
      invalidateMappingQueries(queryClient, variables.projectId);
    },
  });
}

export function useCreateCharacter() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ projectId, name, gender }: {
      projectId: number; name: string; gender?: string | null;
    }) => {
      const { data } = await client.post<ProjectCharacterWithSpeakers>(
        `/projects/${projectId}/characters`,
        { name, gender },
      );
      return data;
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['projects', variables.projectId, 'characters'] });
    },
  });
}

export function useRefreshMetadata() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (projectId: number) => {
      const { data } = await client.post<{
        characters_created: number;
        characters_updated: number;
        episodes_created: number;
        episodes_updated: number;
      }>(`/projects/${projectId}/refresh-metadata`);
      return data;
    },
    onSuccess: (_data, projectId) => {
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'characters'] });
      queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'speakers'] });
    },
  });
}

export function useRetranslateAffected() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ projectId, speakerId }: { projectId: number; speakerId: number }) => {
      const { data } = await client.post<SpeakerUpdateResult>(
        `/projects/${projectId}/speakers/${speakerId}/retranslate-affected`,
      );
      return data;
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['projects', variables.projectId, 'files'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}
