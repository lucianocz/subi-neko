import { Box, Button, Group, Loader, Paper, Stack, Text, Tooltip } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { CheckCircle, Hourglass, Play, RocketLaunch, Stop, Warning } from '@phosphor-icons/react';
import { isAxiosError } from 'axios';
import type { Project, VideoFile } from '../../types';
import { useProjectSpeakers } from '../../hooks/useCharacterMapping';
import { useContextStatus, useProjectStats, usePublishProject } from '../../hooks/useProjects';

// ─── State helpers ────────────────────────────────────────────────────────────

type StepState = 'waiting' | 'active' | 'needs_attention' | 'completed' | 'failed';

const STATUS_RANK: Record<string, number> = {
  new: 0,
  discovering: 1,
  context_review: 2,
  processing: 3,
  review_required: 4,
  completed: 5,
  failed: -1,
};

const STATE_COLOR: Record<StepState, string> = {
  waiting: 'var(--mantine-color-dark-3)',
  active: 'var(--mantine-color-blue-4)',
  needs_attention: 'var(--mantine-color-yellow-5)',
  completed: 'var(--mantine-color-green-5)',
  failed: 'var(--mantine-color-red-5)',
};

const STATE_LABEL: Record<StepState, string> = {
  waiting: 'Waiting',
  active: 'Processing',
  needs_attention: 'Needs attention',
  completed: 'Completed',
  failed: 'Failed',
};

function StepIcon({ state, size = 14 }: { state: StepState; size?: number }) {
  const color = STATE_COLOR[state];
  if (state === 'waiting') return <Hourglass size={size} color={color} />;
  if (state === 'active') return <Play size={size} color={color} weight="fill" />;
  if (state === 'needs_attention' || state === 'failed') {
    return <Warning size={size} color={color} weight="fill" />;
  }
  return <CheckCircle size={size} color={color} weight="fill" />;
}

// ─── Single step box ──────────────────────────────────────────────────────────

interface StepBoxProps {
  title: string;
  state: StepState;
  detail: React.ReactNode;
  /** Overrides the generic label for the state (e.g. "Ready to publish"). */
  label?: string;
}

function StepBox({ title, state, detail, label }: StepBoxProps) {
  const color = STATE_COLOR[state];
  return (
    <Paper
      p="sm"
      withBorder
      style={{
        flex: 1,
        minWidth: 0,
        opacity: state === 'waiting' ? 0.5 : 1,
        borderColor: state === 'waiting' ? 'var(--mantine-color-dark-5)' : undefined,
      }}
    >
      <Stack gap={6}>
        <Text size="xs" fw={700} tt="uppercase" c="dimmed" style={{ letterSpacing: '0.05em' }}>
          {title}
        </Text>
        <Group gap={5} wrap="nowrap">
          <StepIcon state={state} />
          <Text size="sm" fw={500} c={color}>{label ?? STATE_LABEL[state]}</Text>
        </Group>
        <Box>{detail}</Box>
      </Stack>
    </Paper>
  );
}

// ─── Main pipeline component ──────────────────────────────────────────────────

interface ProjectPipelineProps {
  project: Project;
  files: VideoFile[];
}

export function ProjectPipeline({ project, files }: ProjectPipelineProps) {
  const rank = STATUS_RANK[project.status] ?? 0;

  const { data: speakers = [] } = useProjectSpeakers(project.id);
  const { data: stats } = useProjectStats(project.id);
  const { data: context } = useContextStatus(project.id);

  // Aggregate counts from files
  const filesTotal = files.length;
  const filesDiscovered = files.filter((f) => f.status !== 'new' && f.status !== 'discovering').length;
  const chunksTotal = files.reduce((s, f) => s + (f.chunks_total ?? 0), 0);
  const chunksDone = files.reduce((s, f) => s + (f.chunks_done ?? 0), 0);

  // ── Preparation ──────────────────────────────────────────────────────────
  const prepState: StepState =
    rank >= 2 ? 'completed' : rank >= 1 ? 'active' : 'waiting';

  const prepDetail = (
    <Text size="xs" c="dimmed">
      {filesDiscovered}/{filesTotal} files
    </Text>
  );

  // ── Context (gate 1 — built automatically, approved explicitly) ──────────
  const mapped = speakers.filter((s) => !s.is_extra && s.character_id !== null);
  const charCount = new Set(mapped.map((s) => s.character_id)).size;
  const nonExtra = speakers.filter((s) => !s.is_extra).length;

  const contextState: StepState =
    context?.state === 'approved'
      ? 'completed'
      : context?.state === 'failed' || context?.state === 'ready_for_review'
        ? 'needs_attention'
        : rank >= 1 ? 'active' : 'waiting';

  const failedComponents = (context?.components ?? [])
    .filter((c) => c.status === 'failed')
    .map((c) => c.key.replace(/_/g, ' '));

  const contextDetail =
    context?.state === 'approved' ? (
      <Text size="xs" c="dimmed">
        {speakers.length === 0
          ? 'no speakers found'
          : `${charCount} characters · ${mapped.length}/${nonExtra} speakers mapped`}
      </Text>
    ) : context?.state === 'failed' ? (
      <Text size="xs" c="red">failed: {failedComponents.join(', ')}</Text>
    ) : context?.state === 'ready_for_review' ? (
      <Text size="xs" c="yellow">awaiting approval</Text>
    ) : contextState === 'active' ? (
      <Text size="xs" c="dimmed">building translation context…</Text>
    ) : (
      <Text size="xs" c="dimmed">—</Text>
    );

  // ── Translation ──────────────────────────────────────────────────────────
  const transState: StepState =
    rank >= 4 ? 'completed' : rank >= 3 ? 'active' : 'waiting';

  const transDetail = (
    <Text size="xs" c="dimmed">
      {chunksDone}/{chunksTotal} chunks
    </Text>
  );

  // ── Review ───────────────────────────────────────────────────────────────
  const reviewState: StepState =
    rank >= 5 ? 'completed' : rank >= 4 ? 'needs_attention' : 'waiting';

  const reviewDetail =
    reviewState !== 'waiting' ? (
      <Group gap={8} wrap="nowrap">
        {(stats?.qa_errors ?? 0) > 0 && (
          <Group gap={3} wrap="nowrap">
            <Stop size={12} color="var(--mantine-color-red-5)" weight="fill" />
            <Text size="xs" c="red">{stats!.qa_errors}</Text>
          </Group>
        )}
        {(stats?.qa_warnings ?? 0) > 0 && (
          <Group gap={3} wrap="nowrap">
            <Warning size={12} color="var(--mantine-color-yellow-5)" weight="fill" />
            <Text size="xs" c="yellow">{stats!.qa_warnings}</Text>
          </Group>
        )}
        {(stats?.qa_errors ?? 0) === 0 && (stats?.qa_warnings ?? 0) === 0 && (
          <Text size="xs" c="dimmed">No issues</Text>
        )}
      </Group>
    ) : (
      <Text size="xs" c="dimmed">—</Text>
    );

  // ── Output (explicit Publish) ────────────────────────────────────────────
  // The backend derives the whole output state; this only renders it.
  const publish = usePublishProject();
  const output = project.output;

  function handlePublish() {
    publish.mutate(project.id, {
      onError: (err) => {
        const detail = isAxiosError(err) ? err.response?.data?.detail : undefined;
        notifications.show({
          color: 'red',
          title: 'Publish failed to start',
          message: typeof detail === 'string' ? detail : detail?.message ?? 'Unexpected error.',
        });
      },
    });
  }

  const publishedAt = output.published_at
    ? new Date(output.published_at + (output.published_at.endsWith('Z') ? '' : 'Z')).toLocaleString()
    : null;

  const publishButton = (text: string, primary: boolean) => (
    <Button
      size="compact-xs"
      mt={4}
      variant={primary ? 'filled' : 'light'}
      leftSection={<RocketLaunch size={12} weight="fill" />}
      loading={publish.isPending}
      onClick={handlePublish}
    >
      {text}
    </Button>
  );

  let outputState: StepState;
  let outputLabel: string | undefined;
  let outputDetail: React.ReactNode;
  switch (output.state) {
    case 'ready':
      outputState = 'needs_attention';
      outputLabel = 'Ready to publish';
      outputDetail = (
        <Stack gap={2} align="flex-start">
          <Text size="xs" c="dimmed">
            {output.published_revision !== null
              ? 'changes since last publish'
              : `${output.accepted_files}/${output.total_files} files accepted`}
          </Text>
          {publishButton('Publish', true)}
        </Stack>
      );
      break;
    case 'publishing':
      outputState = 'active';
      outputLabel = 'Publishing…';
      outputDetail = (
        <Group gap={6} wrap="nowrap">
          <Loader size={12} />
          <Text size="xs" c="dimmed">{output.total_files} files</Text>
        </Group>
      );
      break;
    case 'published':
      outputState = 'completed';
      outputLabel = 'Published';
      outputDetail = (
        <Stack gap={2} align="flex-start">
          {publishedAt && <Text size="xs" c="dimmed">{publishedAt}</Text>}
          {publishButton('Publish again', false)}
        </Stack>
      );
      break;
    case 'failed':
      outputState = 'failed';
      outputLabel = 'Publish failed';
      outputDetail = (
        <Stack gap={2} align="flex-start">
          <Tooltip label={output.error} disabled={!output.error} multiline maw={420} withArrow>
            <Text size="xs" c="red" lineClamp={2}>{output.error ?? 'Unknown error'}</Text>
          </Tooltip>
          {publishButton('Retry publish', true)}
        </Stack>
      );
      break;
    default:
      outputState = 'waiting';
      outputLabel = 'Not ready';
      outputDetail = (
        <Text size="xs" c="dimmed">
          {output.accepted_files} / {output.total_files} files accepted
        </Text>
      );
  }

  return (
    <Group gap="xs" align="stretch" wrap="nowrap">
      <StepBox title="Preparation" state={prepState} detail={prepDetail} />
      <StepBox title="Context" state={contextState} detail={contextDetail} />
      <StepBox title="Translation" state={transState} detail={transDetail} />
      <StepBox title="Review" state={reviewState} detail={reviewDetail} />
      <StepBox title="Output" state={outputState} detail={outputDetail} label={outputLabel} />
    </Group>
  );
}
