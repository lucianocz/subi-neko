import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { notifications } from '@mantine/notifications';
import client from '../api/client';
import { optionsErrorMessage } from '../utils/optionsValidation';

export type OptionsMap = Record<string, string | null>;

export function useOptions() {
  return useQuery<OptionsMap>({
    queryKey: ['options'],
    queryFn: async () => {
      const { data } = await client.get<OptionsMap>('/options');
      return data;
    },
  });
}

export function useSaveOptions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (patch: OptionsMap) => {
      await client.patch('/options', patch);
    },
    onSuccess: (_data, patch) => {
      // Setting a key to null means "revert to the built-in default", whose
      // value only the server knows — refetch instead of caching the null
      // (which left prompt fields empty until a manual refresh).
      const hasReset = Object.values(patch).some((value) => value === null);
      if (hasReset) {
        queryClient.invalidateQueries({ queryKey: ['options'] });
        return;
      }
      queryClient.setQueryData<OptionsMap>(['options'], (prev) =>
        prev ? { ...prev, ...patch } : patch
      );
    },
    onError: (err: unknown) => {
      const msg = optionsErrorMessage(err);
      notifications.show({
        color: 'red',
        title: 'Failed to save',
        message: msg,
      });
    },
  });
}
