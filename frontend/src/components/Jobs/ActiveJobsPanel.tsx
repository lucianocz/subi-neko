import { useEffect, useRef, useState } from 'react';
import { ActionIcon, Badge, Box, Progress, ScrollArea, Stack, Text, Tooltip } from '@mantine/core';
import { X } from '@phosphor-icons/react';
import type { Job } from '../../types';
import { useActiveJobs } from '../../hooks/useActiveJobs';
import { useJobStats, type JobTypeStat } from '../../hooks/useJobStats';
import client from '../../api/client';

const MIN_SAMPLES = 3;

const JOB_LABELS: Record<string, string> = {
  scan_project: 'Scan',
  inspect_mkv: 'Inspect MKV',
  extract_subtitles: 'Extract subtitles',
  aggregate_speakers: 'Aggregate speakers',
  plan_translation_chunks: 'Plan chunks',
  translate_chunk: 'Translate',
  validate_chunk: 'Validate',
  repair_chunk: 'Repair',
  polish_chunk: 'Polish',
  review_chunk_final: 'Final review',
  resolve_style_fonts: 'Resolve fonts',
  publish_project: 'Publish',
  // Retired per-file output jobs — still labelled for old job history.
  render_output_ass: 'Render ASS',
  mux_output_mkv: 'Mux MKV',
};

const CHUNK_JOB_TYPES = new Set([
  'translate_chunk', 'validate_chunk', 'repair_chunk',
  'polish_chunk', 'review_chunk_final',
]);

function jobLabel(job: Job): string {
  const base = JOB_LABELS[job.job_type] ?? job.job_type.replace(/_/g, ' ');
  if (CHUNK_JOB_TYPES.has(job.job_type) && job.payload_json) {
    try {
      const payload = JSON.parse(job.payload_json);
      if (payload.chunk_index != null) return `${base} #${payload.chunk_index + 1}`;
    } catch { /* ignore */ }
  }
  return base;
}


function JobRow({ job, stats }: { job: Job; stats: Record<string, JobTypeStat> }) {
  const label = jobLabel(job);
  const isRunning = job.status === 'running';
  const [elapsed, setElapsed] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (!isRunning || !job.started_at) return;
    const startMs = new Date(job.started_at.endsWith('Z') ? job.started_at : job.started_at + 'Z').getTime();
    const tick = () => setElapsed((Date.now() - startMs) / 1000);
    tick();
    intervalRef.current = setInterval(tick, 1000);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [isRunning, job.started_at]);

  const stat = stats[job.job_type];
  const hasHistory = stat && stat.sample_count >= MIN_SAMPLES;

  // Pure time-based estimate. Handler-reported progress intentionally ignored —
  // it stalls during external calls making the bar misleading.
  let progressValue: number | null = null;
  if (isRunning && hasHistory) {
    progressValue = Math.min(elapsed / stat.avg_secs, 1.0) * 100;
  }
  // null = indeterminate: queued, or running with insufficient history

  const indeterminate = isRunning && progressValue === null;

  async function handleStop() {
    setStopping(true);
    try {
      await client.post(`/jobs/${job.id}/cancel`);
    } catch {
      setStopping(false);
    }
  }

  return (
    <Box>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 3 }}>
        <Box style={{ flex: 1, minWidth: 0 }}>
          {job.project_name && (
            <Text size="xs" c="dimmed" truncate lh={1.2}>
              {job.project_name}
            </Text>
          )}
          <Text size="xs" fw={500} truncate lh={1.4}>
            {label}
          </Text>
        </Box>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0, marginLeft: 6, marginTop: 2 }}>
          <Badge size="xs" variant="dot" color={isRunning ? 'blue' : 'gray'}>
            {job.status}
          </Badge>
          <Tooltip label="Stop job" withArrow position="left" openDelay={400}>
            <ActionIcon
              size={14}
              variant="subtle"
              color="red"
              loading={stopping}
              onClick={handleStop}
              aria-label="Stop job"
            >
              <X size={10} weight="bold" />
            </ActionIcon>
          </Tooltip>
        </div>
      </div>
      <Progress
        value={indeterminate ? 100 : (progressValue ?? 0)}
        size="xs"
        animated={isRunning}
        striped={indeterminate}
        color={isRunning ? 'blue' : 'gray'}
        mb={job.message ? 2 : 0}
      />
      {job.message && (
        <Text size="xs" c="dimmed" truncate>
          {job.message}
        </Text>
      )}
    </Box>
  );
}

export function ActiveJobsPanel() {
  const { jobs, hiddenCount } = useActiveJobs();
  const stats = useJobStats();

  if (jobs.length === 0 && hiddenCount === 0) return null;

  return (
    <Box
      pt="xs"
      style={{ borderTop: '1px solid var(--mantine-color-dark-4)' }}
    >
      <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb="xs" style={{ letterSpacing: '0.05em' }}>
        Active Jobs
      </Text>
      <ScrollArea mah={160} offsetScrollbars={false}>
        <Stack gap={8}>
          {jobs.map((job) => (
            <JobRow key={job.id} job={job} stats={stats} />
          ))}
        </Stack>
      </ScrollArea>
      {hiddenCount > 0 && (
        <Text size="xs" c="dimmed" mt={6} ta="center">
          +{hiddenCount} more queued
        </Text>
      )}
    </Box>
  );
}
