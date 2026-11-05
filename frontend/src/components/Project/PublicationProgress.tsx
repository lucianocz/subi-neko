import { useState } from 'react';
import { Box, Progress, Stack, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import client from '../../api/client';
import type { WebState } from '../../hooks/useWebPublishing';
import { advanceWorkProgress, operationStatus, publicationLines } from '../../utils/publicationWorkflow';

/** One actual job-work progress bar and a retained terminal log, shared by both wizards. */
export function PublicationProgress({ state }: { state: WebState }) {
  const running = state.state === 'publishing';
  const job = useQuery({ queryKey: ['jobs', state.job?.id], enabled: !!state.job?.id,
    queryFn: async () => (await client.get<{ progress: number | null; message: string | null }>(`/jobs/${state.job?.id}`)).data,
    refetchInterval: running ? 1000 : false });
  const result = state.job?.result;
  const lines = publicationLines(result, running, job.data?.message);
  const done = lines.filter(l => !['running', 'pending'].includes(l.outcome)).length;
  const sample = state.job?.status === 'completed' ? 100 : Math.max(job.data?.progress ?? 0, state.job?.progress ?? 0, result?.progress ?? 0) * 100;
  const [high, setHigh] = useState({ jobId: state.job?.id ?? null, value: sample });
  const next = advanceWorkProgress(high, state.job?.id ?? null, sample);
  if (high.jobId !== next.jobId || high.value !== next.value) setHigh(next);
  const value = next.value;
  return <Stack gap="xs" data-testid="publication-progress">
    <Progress value={value} aria-label="Publication work progress" animated={running} />
    <Box role="log" aria-label="Publication operations" aria-live="polite" tabIndex={0}
      style={{ maxHeight: 300, minHeight: 120, overflow: 'auto', background: 'var(--mantine-color-dark-9)', borderRadius: 4, padding: 12, fontFamily: 'monospace', fontSize: 12 }}>
      {lines.length ? lines.map((line, i) => <Box key={`${i}:${line.stage}`} c={line.outcome === 'failed' ? 'red.4' : line.outcome === 'running' ? 'blue.3' : undefined}
        style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{line.stage} {'.'.repeat(Math.max(3, 36 - line.stage.length))} {operationStatus(line.outcome)}{line.error ? ` — ${line.error}` : ''}</Box>) : 'Waiting to start…'}
    </Box>
    <Text role="status" size="xs" c="dimmed">{running ? 'Publishing' : state.job ? result?.error || ['failed', 'partial'].includes(state.state) ? 'Publication finished with errors' : 'Publication finished' : 'Publication has not started'} · {done} operations recorded</Text>
    {state.job?.error && !result?.error && <Text size="sm" c="red">{state.job.error}</Text>}
  </Stack>;
}
