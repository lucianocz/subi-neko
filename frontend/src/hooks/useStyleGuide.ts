import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../api/client';

export interface GlossaryTerm {
  id: number;
  source_term: string;
  target_term: string;
  category: string;
  gender: string | null;
  vocative: string | null;
  note: string | null;
  origin: string;
  locked: boolean;
  is_active: boolean;
  is_new: boolean;
}

export interface CharacterVoice {
  id: number;
  character_id: number;
  character_name: string;
  voice_note: string | null;
  register: string | null;
  origin: string;
  locked: boolean;
}

export interface AddressPair {
  id: number;
  speaker_name: string;
  addressee_name: string;
  mode: 'tykani' | 'vykani' | 'mixed';
  origin: string;
  locked: boolean;
  is_new: boolean;
}

export interface StyleGuide {
  version: number | null;
  tone_summary: string | null;
  register_notes: string | null;
  honorific_policy: string | null;
  character_voices: CharacterVoice[];
  address_pairs: AddressPair[];
}

export const GLOSSARY_CATEGORIES = [
  'name', 'place', 'technique', 'item', 'honorific', 'catchphrase', 'other',
] as const;

export function useStyleGuide(projectId: number, enabled: boolean) {
  return useQuery<StyleGuide>({
    queryKey: ['projects', projectId, 'style-guide'],
    queryFn: async () => {
      const { data } = await client.get<StyleGuide>(`/projects/${projectId}/style-guide`);
      return data;
    },
    enabled,
    staleTime: 30_000,
  });
}

export function useGlossaryTerms(projectId: number, enabled: boolean) {
  return useQuery<GlossaryTerm[]>({
    queryKey: ['projects', projectId, 'glossary'],
    queryFn: async () => {
      const { data } = await client.get<GlossaryTerm[]>(`/projects/${projectId}/glossary`);
      return data;
    },
    enabled,
    staleTime: 30_000,
  });
}

export function useCreateGlossaryTerm(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      source_term: string;
      target_term: string;
      category: string;
      gender?: string | null;
      vocative?: string | null;
      note?: string | null;
    }) => {
      const { data } = await client.post<GlossaryTerm>(`/projects/${projectId}/glossary`, body);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'glossary'] }),
  });
}

export function useUpdateGlossaryTerm(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ termId, ...body }: {
      termId: number;
      target_term?: string;
      category?: string;
      gender?: string | null;
      vocative?: string | null;
      note?: string | null;
      is_active?: boolean;
    }) => {
      const { data } = await client.put<GlossaryTerm>(`/projects/${projectId}/glossary/${termId}`, body);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'glossary'] }),
  });
}

export function useDeleteGlossaryTerm(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (termId: number) => {
      await client.delete(`/projects/${projectId}/glossary/${termId}`);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'glossary'] }),
  });
}

export function useUpdateStyleBible(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      tone_summary?: string | null;
      register_notes?: string | null;
      honorific_policy?: string | null;
    }) => {
      const { data } = await client.put<StyleGuide>(`/projects/${projectId}/style-bible`, body);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'style-guide'] }),
  });
}

export function useUpdateAddressPair(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ pairId, mode }: { pairId: number; mode: AddressPair['mode'] }) => {
      const { data } = await client.put<AddressPair>(`/projects/${projectId}/address-pairs/${pairId}`, { mode });
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'style-guide'] }),
  });
}

export interface TmEntry {
  id: number;
  source_text: string;
  target_text: string;
  content_type: string;
  origin: 'human' | 'ai';
  use_count: number;
  updated_at: string;
}

export interface TmListPage {
  total: number;
  items: TmEntry[];
}

const TM_PAGE_SIZE = 200;

export function useTranslationMemory(projectId: number, enabled: boolean, query: string) {
  return useInfiniteQuery<TmListPage>({
    queryKey: ['projects', projectId, 'translation-memory', query],
    queryFn: async ({ pageParam }) => {
      const { data } = await client.get<TmListPage>(
        `/projects/${projectId}/translation-memory`,
        {
          params: {
            ...(query ? { q: query } : {}),
            offset: pageParam as number,
            limit: TM_PAGE_SIZE,
          },
        },
      );
      return data;
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const fetched = allPages.reduce((n, p) => n + p.items.length, 0);
      return fetched < lastPage.total ? fetched : undefined;
    },
    enabled,
    staleTime: 10_000,
  });
}

export function useUpdateTmEntry(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ entryId, targetText }: { entryId: number; targetText: string }) => {
      const { data } = await client.put<TmEntry>(
        `/projects/${projectId}/translation-memory/${entryId}`,
        { target_text: targetText },
      );
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({
      queryKey: ['projects', projectId, 'translation-memory'],
    }),
  });
}

export function useDeleteTmEntry(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (entryId: number) => {
      await client.delete(`/projects/${projectId}/translation-memory/${entryId}`);
    },
    onSuccess: () => queryClient.invalidateQueries({
      queryKey: ['projects', projectId, 'translation-memory'],
    }),
  });
}

export function useUpdateCharacterVoice(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ styleId, ...body }: { styleId: number; voice_note?: string | null; register?: string | null }) => {
      const { data } = await client.put<CharacterVoice>(`/projects/${projectId}/character-styles/${styleId}`, body);
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'style-guide'] }),
  });
}
