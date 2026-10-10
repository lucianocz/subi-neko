import type { QueryClient } from '@tanstack/react-query';

/**
 * Refetch everything derived from the speaker→character mapping of ONE project:
 * the characters/speakers lists and the Translation context panel (mapping
 * confidence, unmapped speakers), which `/context-status` computes live.
 *
 * Invalidation only marks queries stale and refetches active ones in place (data
 * stays on screen, so no BUILDING flicker); an in-flight refetch is cancelled
 * and restarted, so a quick second mutation can't be overwritten by the first
 * one's older response. Call it from a mutation's `onSuccess` only.
 */
export function invalidateMappingQueries(queryClient: QueryClient, projectId: number) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'characters'] }),
    queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'speakers'] }),
    queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'context'] }),
  ]);
}
