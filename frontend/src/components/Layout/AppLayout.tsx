import { useState } from 'react';
import {
  ActionIcon,
  AppShell,
  Badge,
  Box,
  Button,
  Card,
  Center,
  Group,
  HoverCard,
  Image,
  Loader,
  Menu,
  Modal,
  ScrollArea,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
  useMantineTheme,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import {
  ArrowClockwiseIcon,
  ArrowCounterClockwise,
  BookOpen,
  CaretDown,
  CaretRight,
  ChartLine,
  CheckCircle,
  Clock,
  Gear,
  Info,
  ListChecks,
  MinusCircle,
  NotePencil,
  Pause,
  Play,
  Plus,
  Eye,
  DownloadSimple,
  DotsThreeVertical,
  SpinnerGap,
  Stop,
  Trash,
  Warning,
  XCircle,
} from '@phosphor-icons/react';
import type { ChunkJob, FileStatus, Project, SubtitleChunk, VideoFile } from '../../types';
import { useProjects, useProjectFiles, useFileChunks, useDeleteProject, usePauseProject, useResumeProject, useRetryChunk, useAcceptFileReview, useTranslateFile, useRetranslateFile } from '../../hooks/useProjects';
import { useRefreshMetadata } from '../../hooks/useCharacterMapping';
import { OptionsDrawer } from '../../pages/OptionsDrawer';
import { ImportDialog } from '../../pages/ImportDialog';
import { MetricsDialog } from '../../pages/MetricsDialog';
import { ReviewQueueDialog } from '../../pages/ReviewQueueDialog';
import { StyleGuideDialog } from '../../pages/StyleGuideDialog';
import { SubtitleEditorDialog } from '../../pages/SubtitleEditorDialog';
import { WatchedWordsDialog } from '../../pages/WatchedWordsDialog';
import { ActiveJobsPanel } from '../Jobs/ActiveJobsPanel';
import { ContextReviewPanel } from '../Project/ContextReviewPanel';
import { ProjectPipeline } from '../Project/ProjectPipeline';
import posterUrl from '../../assets/poster.png';

// ─── Status dot colors ────────────────────────────────────────────────────────

function getProjectDotColor(project: Project): string {
  if (project.status === 'failed') return 'var(--mantine-color-red-5)';
  if (project.is_paused || project.status === 'review_required')
    return 'var(--mantine-color-yellow-5)';
  if (project.status === 'completed') return 'var(--mantine-color-green-5)';
  return 'var(--mantine-color-cyan-4)'; // new / discovering / processing
}

// ─── Status badge colors ──────────────────────────────────────────────────────

const FILE_STATUS_COLORS: Record<FileStatus, string> = {
  new: 'gray',
  discovering: 'cyan',
  waiting: 'yellow',
  ready: 'blue',
  processing: 'indigo',
  review_required: 'orange',
  accepted: 'teal',
  muxing: 'violet',
  completed: 'green',
  paused: 'gray',
  failed: 'red',
};

const PROJECT_STATUS_COLORS: Record<string, string> = {
  new: 'gray',
  discovering: 'cyan',
  context_review: 'yellow',
  processing: 'blue',
  review_required: 'orange',
  completed: 'green',
  failed: 'red',
};

function directoryName(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  return normalized.split('/').pop() || path;
}

function seriesUrl(project: Project): string | null {
  if (project.anime_provider === 'anilist') return `https://anilist.co/anime/${project.anime_external_id}`;
  if (project.anime_provider === 'anidb') return `https://anidb.net/anime/${project.anime_external_id}`;
  return null;
}

// ─── Chunk panel ──────────────────────────────────────────────────────────────

type PipelineTone = 'waiting' | 'queued' | 'processing' | 'done' | 'failed' | 'not-needed' | 'issues';

interface PipelineBadge {
  label: string;
  tone: PipelineTone;
  jobType?: string;
  job?: ChunkJob;
}

const PIPELINE_COLORS: Record<PipelineTone, string> = {
  waiting: 'gray',
  queued: 'gray',
  processing: 'blue',
  done: 'green',
  failed: 'red',
  'not-needed': 'gray',
  issues: 'yellow',
};

const CONTENT_TYPE_COLORS: Record<string, string> = {
  sign: 'grape',
  song: 'cyan',
  karaoke: 'teal',
};

const COMPLETE_AFTER_VALIDATE = new Set(['validated', 'polished', 'needs_polish', 'final_reviewed', 'audited', 'complete']);
const COMPLETE_AFTER_POLISH = new Set(['polished', 'final_reviewed', 'audited', 'complete']);
const COMPLETE_AFTER_FINAL = new Set(['final_reviewed', 'audited', 'complete']);
const COMPLETE_AFTER_AUDIT = new Set(['audited', 'complete']);

function isProcessing(job?: ChunkJob) {
  return job?.status === 'running';
}

function isQueued(job?: ChunkJob) {
  return job?.status === 'queued';
}

function isCompleted(job?: ChunkJob) {
  return job?.status === 'completed';
}

function numberResult(job: ChunkJob | undefined, key: string): number | null {
  const value = job?.result?.[key];
  return typeof value === 'number' ? value : null;
}

function labelWithRuns(label: string, job?: ChunkJob) {
  return job && job.attempt_count > 1 ? `${label} (${job.attempt_count}x)` : label;
}

function statusTooltip(badge: PipelineBadge) {
  const parts = [badge.jobType ? `Job: ${badge.jobType}` : null];
  if (badge.job) {
    const lastRun = badge.job.finished_at ?? badge.job.started_at ?? badge.job.scheduled_at;
    parts.push(`Status: ${badge.job.status}`);
    parts.push(`Attempts: ${badge.job.attempt_count}`);
    parts.push(`Last run: ${new Date(lastRun).toLocaleString()}`);
    if (badge.job.error_code) parts.push(`Error: ${badge.job.error_code}`);
    if (badge.job.error_message) parts.push(badge.job.error_message);
  }
  return parts.filter(Boolean).join('\n') || badge.label;
}

function PipelineStatus({ badge }: { badge: PipelineBadge }) {
  const color = PIPELINE_COLORS[badge.tone];
  const iconColor = `var(--mantine-color-${color}-5)`;
  const Icon = {
    waiting: Clock,
    queued: Clock,
    processing: SpinnerGap,
    done: CheckCircle,
    failed: XCircle,
    'not-needed': MinusCircle,
    issues: Warning,
  }[badge.tone];

  return (
    <Group gap={4} wrap="nowrap" title={statusTooltip(badge)}>
      <Icon size={13} color={iconColor} weight={badge.tone === 'processing' ? 'bold' : 'regular'} />
      <Text size="xs" c={color} lh={1} truncate>
        {badge.label}
      </Text>
    </Group>
  );
}

function chunkJob(chunk: SubtitleChunk, jobType: string) {
  return chunk.jobs?.[jobType];
}

function translateBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'translate_chunk');
  if (job?.status === 'failed') return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'translate_chunk', job };
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'translate_chunk', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'translate_chunk', job };
  if (isCompleted(job) || chunk.status !== 'pending') return { label: labelWithRuns('Done', job), tone: 'done', jobType: 'translate_chunk', job };
  return { label: 'Waiting', tone: 'waiting', jobType: 'translate_chunk', job };
}

function validateBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'validate_chunk');
  if (job?.status === 'failed') {
    if (job.result?.valid === true && COMPLETE_AFTER_VALIDATE.has(chunk.status)) {
      return { label: labelWithRuns('Done', job), tone: 'done', jobType: 'validate_chunk', job };
    }
    if (job.result?.valid === false) {
      return { label: labelWithRuns('Rejected', job), tone: 'issues', jobType: 'validate_chunk', job };
    }
    return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'validate_chunk', job };
  }
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'validate_chunk', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'validate_chunk', job };
  if (isCompleted(job)) {
    const valid = job.result?.valid;
    if (valid === false) return { label: labelWithRuns('Rejected', job), tone: 'issues', jobType: 'validate_chunk', job };
    return { label: labelWithRuns('Done', job), tone: 'done', jobType: 'validate_chunk', job };
  }
  if (COMPLETE_AFTER_VALIDATE.has(chunk.status)) return { label: labelWithRuns('Done', job), tone: 'done', jobType: 'validate_chunk', job };
  return { label: 'Waiting', tone: 'waiting', jobType: 'validate_chunk', job };
}

function fixBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'repair_chunk');
  if (job?.status === 'failed') return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'repair_chunk', job };
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'repair_chunk', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'repair_chunk', job };
  if (isCompleted(job)) return { label: labelWithRuns('Done', job), tone: 'done', jobType: 'repair_chunk', job };
  if (validateBadge(chunk).tone === 'done') return { label: 'Not needed', tone: 'not-needed', jobType: 'repair_chunk', job };
  return { label: 'Waiting', tone: 'waiting', jobType: 'repair_chunk', job };
}

function polishBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'polish_chunk');
  const edits = numberResult(job, 'edits_applied') ?? 0;
  if (job?.status === 'failed') return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'polish_chunk', job };
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'polish_chunk', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'polish_chunk', job };
  if (chunk.status === 'needs_polish') return { label: 'Re-polish', tone: 'issues', jobType: 'polish_chunk', job };
  if (!COMPLETE_AFTER_VALIDATE.has(chunk.status)) return { label: 'Waiting', tone: 'waiting', jobType: 'polish_chunk', job };
  if (COMPLETE_AFTER_POLISH.has(chunk.status)) {
    return edits > 0
      ? { label: labelWithRuns(`Done (${edits} edits)`, job), tone: 'done', jobType: 'polish_chunk', job }
      : { label: labelWithRuns('Done', job), tone: 'done', jobType: 'polish_chunk', job };
  }
  return { label: 'Waiting', tone: 'waiting', jobType: 'polish_chunk', job };
}

function finalBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'review_chunk_final');
  const warnings = numberResult(job, 'warnings_created') ?? 0;
  if (job?.status === 'failed') return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'review_chunk_final', job };
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'review_chunk_final', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'review_chunk_final', job };
  if (chunk.status === 'needs_polish') return { label: `Flagged (${warnings})`, tone: 'issues', jobType: 'review_chunk_final', job };
  if (!COMPLETE_AFTER_POLISH.has(chunk.status)) return { label: 'Waiting', tone: 'waiting', jobType: 'review_chunk_final', job };
  if (COMPLETE_AFTER_FINAL.has(chunk.status)) {
    return warnings > 0
      ? { label: `Issues (${warnings})`, tone: 'issues', jobType: 'review_chunk_final', job }
      : { label: labelWithRuns('Done', job), tone: 'done', jobType: 'review_chunk_final', job };
  }
  return { label: 'Waiting', tone: 'waiting', jobType: 'review_chunk_final', job };
}

function auditBadge(chunk: SubtitleChunk): PipelineBadge {
  const job = chunkJob(chunk, 'audit_chunk_final');
  if (chunk.content_type !== 'dialogue') {
    return { label: 'Not needed', tone: 'not-needed', jobType: 'audit_chunk_final', job };
  }
  const findings = numberResult(job, 'findings_created') ?? 0;
  if (job?.status === 'failed') return { label: labelWithRuns('Failed', job), tone: 'failed', jobType: 'audit_chunk_final', job };
  if (isProcessing(job)) return { label: 'Processing', tone: 'processing', jobType: 'audit_chunk_final', job };
  if (isQueued(job)) return { label: 'Queued', tone: 'queued', jobType: 'audit_chunk_final', job };
  if (isCompleted(job) || COMPLETE_AFTER_AUDIT.has(chunk.status)) {
    return findings > 0
      ? { label: `Issues (${findings})`, tone: 'issues', jobType: 'audit_chunk_final', job }
      : { label: labelWithRuns('Done', job), tone: 'done', jobType: 'audit_chunk_final', job };
  }
  return { label: 'Waiting', tone: 'waiting', jobType: 'audit_chunk_final', job };
}

function isQaCompleted(chunk: SubtitleChunk) {
  return Boolean(
    chunkJob(chunk, 'validate_chunk')?.status === 'completed'
    || chunkJob(chunk, 'polish_chunk')?.status === 'completed'
    || chunkJob(chunk, 'review_chunk_final')?.status === 'completed'
    || chunkJob(chunk, 'audit_chunk_final')?.status === 'completed'
    || COMPLETE_AFTER_VALIDATE.has(chunk.status)
  );
}

function IssuesCell({ chunk }: { chunk: SubtitleChunk }) {
  if (!isQaCompleted(chunk)) {
    return <PipelineStatus badge={{ label: 'Waiting', tone: 'waiting' }} />;
  }

  if (chunk.qa_errors === 0 && chunk.qa_warnings === 0) {
    return <Text size="xs" c="dimmed">0</Text>;
  }

  return (
    <Group gap={6} wrap="nowrap">
      {chunk.qa_errors > 0 && (
        <Group gap={2} wrap="nowrap">
          <Stop size={12} color="var(--mantine-color-red-5)" weight="fill" />
          <Text size="xs" c="red" lh={1}>{chunk.qa_errors}</Text>
        </Group>
      )}
      {chunk.qa_warnings > 0 && (
        <Group gap={2} wrap="nowrap">
          <Warning size={12} color="var(--mantine-color-yellow-5)" weight="fill" />
          <Text size="xs" c="yellow" lh={1}>{chunk.qa_warnings}</Text>
        </Group>
      )}
    </Group>
  );
}

function FileIssuesCell({ file }: { file: VideoFile }) {
  if (file.qa_issues <= 0) return <Text size="xs" c="dimmed">—</Text>;
  return (
    <Group gap={6} wrap="nowrap">
      {file.qa_errors > 0 && (
        <Group gap={2} wrap="nowrap">
          <Stop size={12} color="var(--mantine-color-red-5)" weight="fill" />
          <Text size="xs" c="red" lh={1}>{file.qa_errors}</Text>
        </Group>
      )}
      {file.qa_warnings > 0 && (
        <Group gap={2} wrap="nowrap">
          <Warning size={12} color="var(--mantine-color-yellow-5)" weight="fill" />
          <Text size="xs" c="yellow" lh={1}>{file.qa_warnings}</Text>
        </Group>
      )}
    </Group>
  );
}

// ─── Chunk status cell (failure states + retry) ───────────────────────────────

const CHUNK_STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending:                { label: 'Pending',            color: 'gray' },
  translated:             { label: 'Translated',         color: 'blue' },
  validated:              { label: 'Validated',          color: 'indigo' },
  needs_polish:           { label: 'Needs polish',       color: 'yellow' },
  polished:               { label: 'Polished',           color: 'violet' },
  final_reviewed:         { label: 'Final reviewed',     color: 'teal' },
  audited:                { label: 'Audited',            color: 'cyan' },
  complete:               { label: 'Complete',           color: 'green' },
  job_failed:             { label: 'Job failed',         color: 'red' },
  validate_trans_failed:  { label: 'Needs repair',       color: 'yellow' },
  validate_repair_failed: { label: 'Validation failed',  color: 'red' },
};

function ChunkStatusBadge({ chunk }: { chunk: SubtitleChunk }) {
  const info = CHUNK_STATUS_LABELS[chunk.status]
    ?? { label: chunk.status.replace(/_/g, ' '), color: 'gray' };

  if (chunk.status === 'job_failed' && chunk.last_error_message) {
    return (
      <HoverCard width={280} shadow="md" withArrow openDelay={200}>
        <HoverCard.Target>
          <Badge color={info.color} variant="light" size="xs" style={{ cursor: 'default' }}>
            {info.label}
          </Badge>
        </HoverCard.Target>
        <HoverCard.Dropdown>
          <Stack gap={4}>
            {chunk.last_error_code && (
              <Text size="xs" fw={600} c="red">{chunk.last_error_code}</Text>
            )}
            <Text size="xs" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {chunk.last_error_message}
            </Text>
          </Stack>
        </HoverCard.Dropdown>
      </HoverCard>
    );
  }

  return (
    <Badge color={info.color} variant="light" size="xs">
      {info.label}
    </Badge>
  );
}

function ChunkRetryButton({
  chunk,
  projectId,
  fileId,
}: {
  chunk: SubtitleChunk;
  projectId: number;
  fileId: number;
}) {
  const retryMutation = useRetryChunk(projectId, fileId);
  const canRetry = chunk.status === 'job_failed' || chunk.status === 'validate_repair_failed';
  if (!canRetry) return null;

  return (
    <Button
      size="compact-xs"
      variant="filled"
      color="blue"
      leftSection={<ArrowClockwiseIcon size={11} />}
      loading={retryMutation.isPending}
      onClick={() => retryMutation.mutate(chunk.chunk_index)}
    >
      Retry
    </Button>
  );
}

function FileChunksPanel({ projectId, fileId }: { projectId: number; fileId: number }) {
  const { data: chunks, isLoading } = useFileChunks(projectId, fileId, true);

  if (isLoading) return <Center py="xs"><Loader size="xs" /></Center>;
  if (!chunks?.length) return <Text size="xs" c="dimmed" py="xs">No chunks yet.</Text>;

  // "sign 2" instead of a wall of identical "sign" badges: per-type ordinal,
  // shown only when a content type has more than one chunk.
  const typeTotals = new Map<string, number>();
  for (const c of chunks as SubtitleChunk[]) {
    typeTotals.set(c.content_type, (typeTotals.get(c.content_type) ?? 0) + 1);
  }
  const typeSeen = new Map<string, number>();
  const badgeLabels = new Map<number, string>();
  for (const c of chunks as SubtitleChunk[]) {
    const ordinal = (typeSeen.get(c.content_type) ?? 0) + 1;
    typeSeen.set(c.content_type, ordinal);
    badgeLabels.set(
      c.id,
      (typeTotals.get(c.content_type) ?? 0) > 1
        ? `${c.content_type} ${ordinal}`
        : c.content_type,
    );
  }

  return (
    <ScrollArea type="auto" offsetScrollbars>
      <Table fz="xs" withColumnBorders={false} style={{ minWidth: 1197, tableLayout: 'fixed' }}>
      <Table.Thead>
        <Table.Tr>
          <Table.Th style={{ width: 40 }}>#</Table.Th>
          <Table.Th style={{ width: 90 }}>Lines</Table.Th>
          <Table.Th style={{ width: 140 }}>Model</Table.Th>
          <Table.Th style={{ width: 92 }}>Translate</Table.Th>
          <Table.Th style={{ width: 92 }}>Validate</Table.Th>
          <Table.Th style={{ width: 92 }}>Fix</Table.Th>
          <Table.Th style={{ width: 110 }}>Polish</Table.Th>
          <Table.Th style={{ width: 92 }}>Final</Table.Th>
          <Table.Th style={{ width: 92 }}>Audit</Table.Th>
          <Table.Th style={{ width: 72 }}>Issues</Table.Th>
          <Table.Th style={{ width: 130 }}>Status</Table.Th>
          <Table.Th style={{ width: 80 }} />
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {chunks.map((c: SubtitleChunk) => (
          <Table.Tr key={c.id}>
            <Table.Td c="dimmed">{c.chunk_index + 1}</Table.Td>
            <Table.Td c="dimmed">
              {c.content_type === 'dialogue' ? (
                `${c.translate_from_line}–${c.translate_to_line}`
              ) : (
                <Badge
                  size="xs"
                  variant="light"
                  color={CONTENT_TYPE_COLORS[c.content_type] ?? 'gray'}
                  title={`Lines ${c.translate_from_line}–${c.translate_to_line}`}
                >
                  {badgeLabels.get(c.id) ?? c.content_type}
                </Badge>
              )}
            </Table.Td>
            <Table.Td c="dimmed" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {c.model ?? '—'}
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={translateBadge(c)} />
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={validateBadge(c)} />
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={fixBadge(c)} />
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={polishBadge(c)} />
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={finalBadge(c)} />
            </Table.Td>
            <Table.Td>
              <PipelineStatus badge={auditBadge(c)} />
            </Table.Td>
            <Table.Td>
              <IssuesCell chunk={c} />
            </Table.Td>
            <Table.Td>
              <ChunkStatusBadge chunk={c} />
            </Table.Td>
            <Table.Td style={{textAlign: 'right', minHeight: '45px', height: '45px'}}>
              <ChunkRetryButton chunk={c} projectId={projectId} fileId={fileId} />
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
      </Table>
    </ScrollArea>
  );
}

// ─── File row ─────────────────────────────────────────────────────────────────

function FileRow({
  file,
  projectId,
  contextApproved,
  expanded,
  onEditSubtitles,
  onToggleExpanded,
  onExpand,
  onCollapse,
  onRetranslate,
}: {
  file: VideoFile;
  projectId: number;
  contextApproved: boolean;
  expanded: boolean;
  onEditSubtitles: (file: VideoFile) => void;
  onToggleExpanded: (fileId: number) => void;
  onExpand: (fileId: number) => void;
  onCollapse: (fileId: number) => void;
  onRetranslate: (file: VideoFile) => void;
}) {
  const showEditButton = file.status === 'processing'
      || (file.status === 'waiting' && (file.blocking_reason === 'validation_failed' || file.blocking_reason === 'translation_failed' ))
      || file.status === 'review_required'
      || file.status === 'accepted'
      || file.status === 'completed';
  const acceptReview = useAcceptFileReview(projectId);
  const translateFile = useTranslateFile(projectId);
  const showAcceptButton = file.status === 'review_required';
  const analysisFailed = file.status === 'waiting' && file.blocking_reason === 'analysis_failed';
  const showTranslateButton =
    (file.status === 'ready' && !file.translation_requested_at) || analysisFailed;
  const canDownloadOriginal = [
    'ready', 'processing', 'waiting', 'review_required', 'accepted', 'muxing', 'completed',
  ].includes(file.status);
  const canDownloadTranslation = ['review_required', 'accepted', 'muxing', 'completed'].includes(file.status);
  const canRetranslate = ['review_required', 'accepted', 'completed'].includes(file.status);

  // `ready` covers both "never started" and "started, running pre-translation
  // gates" — render the distinction instead of the raw status.
  const statusLabel = file.status === 'ready'
    ? (file.translation_requested_at ? 'preparing' : 'awaiting translation')
    : file.status.replace(/_/g, ' ');

  return (
    <>
      <Table.Tr style={{ cursor: 'pointer' }} onClick={() => onToggleExpanded(file.id)}>
        <Table.Td>
          <Text size="sm" truncate>{file.filename}</Text>
        </Table.Td>
        <Table.Td>
          {file.last_error_code ? (
            <Text size="xs" c="red" truncate title={file.last_error_message ?? undefined}>
              {file.last_error_code}
            </Text>
          ) : file.chunks_total != null ? (
            <Text size="xs" c="dimmed">{file.chunks_done ?? 0}/{file.chunks_total}</Text>
          ) : (
            <Text size="xs" c="dimmed">—</Text>
          )}
        </Table.Td>
        <Table.Td>
          <Badge color={FILE_STATUS_COLORS[file.status]} variant="light" size="sm">
            {statusLabel}
          </Badge>
        </Table.Td>
        <Table.Td>
          <Text size="xs" c="dimmed">{file.detected_subtitle_format ?? '—'}</Text>
        </Table.Td>
        <Table.Td>
          <Text size="xs" c="dimmed">{new Date(file.updated_at).toLocaleString()}</Text>
        </Table.Td>
        <Table.Td>
          <FileIssuesCell file={file} />
        </Table.Td>
        <Table.Td style={{ width: 224, textAlign: 'right', minHeight: '45px', height: '45px' }}>
          <Group gap={6} justify="flex-end" wrap="nowrap">
            {showTranslateButton && (
              <Tooltip
                label="Approve the translation context first"
                disabled={contextApproved}
                withArrow
              >
                <Button
                  size="xs"
                  variant="filled"
                  color={analysisFailed ? 'red' : 'blue'}
                  leftSection={analysisFailed ? <ArrowClockwiseIcon size={13} /> : <Play size={13} />}
                  loading={translateFile.isPending}
                  disabled={!contextApproved}
                  title={analysisFailed
                    ? `Script analysis failed (${file.last_error_code ?? 'error'}) — retry`
                    : 'Start translating this file'}
                  onClick={(e) => {
                    e.stopPropagation();
                    translateFile.mutate(file.id);
                    onExpand(file.id);
                  }}
                >
                  {analysisFailed ? 'Retry' : 'Translate'}
                </Button>
              </Tooltip>
            )}
            {showAcceptButton && (
              <Button
                size="xs"
                variant="filled"
                color="green"
                leftSection={<CheckCircle size={13} />}
                loading={acceptReview.isPending}
                disabled={file.qa_errors > 0}
                title={
                  file.qa_errors > 0
                    ? 'Resolve blocker issues before accepting review'
                    : file.qa_warnings > 0
                      ? `Accept and resolve ${file.qa_warnings} remaining warning(s)`
                      : 'Accept review; output starts after every file is accepted'
                }
                onClick={(e) => {
                  e.stopPropagation();
                  if (
                    file.qa_warnings > 0
                    && !window.confirm(`Accept with ${file.qa_warnings} unresolved warning(s)? They will be marked resolved.`)
                  ) {
                    return;
                  }
                  acceptReview.mutate(
                    { fileId: file.id, resolveWarnings: file.qa_warnings > 0 },
                    { onSuccess: () => onCollapse(file.id) },
                  );
                }}
              >
                Accept
              </Button>
            )}
            {showEditButton && (
              <Button
                size="xs"
                variant="outline"
                color="pink"
                leftSection={<NotePencil size={13} />}
                onClick={(e) => {
                  e.stopPropagation();
                  onEditSubtitles(file);
                }}
              >
                Edit
              </Button>
            )}
            <Menu position="bottom-end" withinPortal shadow="md">
              <Menu.Target>
                <ActionIcon
                  variant="subtle"
                  color="gray"
                  aria-label={`More actions for ${file.filename}`}
                  title="More actions"
                  onClick={(event) => event.stopPropagation()}
                >
                  <DotsThreeVertical size={17} weight="bold" />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown onClick={(event) => event.stopPropagation()}>
                <Menu.Item
                  leftSection={<DownloadSimple size={15} />}
                  disabled={!canDownloadOriginal}
                  onClick={() => window.location.assign(`/api/projects/${projectId}/files/${file.id}/subtitles/original`)}
                >
                  Download original
                </Menu.Item>
                <Menu.Item
                  leftSection={<DownloadSimple size={15} />}
                  disabled={!canDownloadTranslation}
                  onClick={() => window.location.assign(`/api/projects/${projectId}/files/${file.id}/subtitles/translated`)}
                >
                  Download translation
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  color="orange"
                  leftSection={<ArrowCounterClockwise size={15} />}
                  disabled={!canRetranslate}
                  onClick={() => onRetranslate(file)}
                >
                  Retranslate
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Table.Td>
        <Table.Td style={{ width: 28, textAlign: 'center' }}>
          <ActionIcon size="xs" variant="subtle" color="gray" onClick={(e) => { e.stopPropagation(); onToggleExpanded(file.id); }}>
            {expanded ? <CaretDown size={12} /> : <CaretRight size={12} />}
          </ActionIcon>
        </Table.Td>
      </Table.Tr>
      {expanded && (
        <Table.Tr>
          <Table.Td colSpan={8} style={{ backgroundColor: 'var(--mantine-color-dark-7)', padding: '10px 16px' }}>
            <FileChunksPanel projectId={projectId} fileId={file.id} />
          </Table.Td>
        </Table.Tr>
      )}
    </>
  );
}

// ─── Components ───────────────────────────────────────────────────────────────

function ProjectCard({
  project,
  selected,
  onClick,
}: {
  project: Project;
  selected: boolean;
  onClick: () => void;
}) {
  const theme = useMantineTheme();

  return (
    <UnstyledButton onClick={onClick} w="100%">
      <Card
        p="sm"
        radius="md"
        withBorder
        style={{
          borderColor: selected ? theme.colors.pink[6] : undefined,
          backgroundColor: selected ? 'var(--mantine-color-dark-5)' : undefined,
          cursor: 'pointer',
        }}
      >
        <Group justify="space-between" gap="xs">
          <Text size="sm" fw={600} truncate style={{ flex: 1 }}>
            {project.name}
          </Text>
          <Box
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              backgroundColor: getProjectDotColor(project),
              flexShrink: 0,
            }}
          />
        </Group>
        <Text size="xs" c="dimmed" truncate mt={2}>
          {directoryName(project.source_directory)}
        </Text>
      </Card>
    </UnstyledButton>
  );
}

function ProjectSidebar({
  projects,
  selectedId,
  onSelect,
  onImport,
}: {
  projects: Project[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  onImport: () => void;
}) {
  return (
    <Stack gap="xs" h="100%">
      <Button leftSection={<Plus size={16} weight="bold" />} fullWidth onClick={onImport}>
        Import
      </Button>

      <ScrollArea style={{ flex: 1 }}>
        <Stack gap="xs" w="100%">
          {projects.map((p) => (
            <ProjectCard
              key={p.id}
              project={p}
              selected={p.id === selectedId}
              onClick={() => onSelect(p.id)}
            />
          ))}
        </Stack>
      </ScrollArea>

      <ActiveJobsPanel />
    </Stack>
  );
}

function EmptyProjectLanding({ isLoading }: { isLoading: boolean }) {
  if (isLoading) {
    return (
      <Center h="100%">
        <Text c="dimmed">Loading…</Text>
      </Center>
    );
  }

  return (
    <Box
      h="100%"
      style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'flex-start',
        paddingTop: 56,
      }}
    >
      <Stack gap="lg" align="center" maw={760} w="100%">
        <Stack gap={8} align="center" ta="center" px="md">
          <Title
            order={1}
            c="pink.2"
            style={{
              fontSize: 28,
              lineHeight: 1.18,
              whiteSpace: 'nowrap',
            }}
          >
            Nyaa… you haven’t brought me anything to work on yet.
          </Title>
          <Text size="lg" c="dimmed" fw={500}>
            Import a project. I’ll take it from here.
          </Text>
        </Stack>

        <Image
          src={posterUrl}
          alt="Subi neko"
		  h={200}
		  w="auto"
        />
      </Stack>
    </Box>
  );
}

function ProjectDetails({ project, onDeleted }: { project: Project; onDeleted: () => void }) {
  const { data: files = [], isLoading } = useProjectFiles(project.id);
  const deleteMutation = useDeleteProject();
  const pauseMutation = usePauseProject();
  const resumeMutation = useResumeProject();
  const refreshMetadata = useRefreshMetadata();
  const retranslateMutation = useRetranslateFile(project.id);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [watchedWordsOpen, setWatchedWordsOpen] = useState(false);
  const [styleGuideOpen, setStyleGuideOpen] = useState(false);
  const [styleGuideTab, setStyleGuideTab] = useState<'glossary' | 'characters' | 'bible' | 'tm'>('glossary');
  const [reviewQueueOpen, setReviewQueueOpen] = useState(false);
  const [metricsOpen, setMetricsOpen] = useState(false);
  const [subtitleEditorFile, setSubtitleEditorFile] = useState<VideoFile | null>(null);
  const [retranslateFile, setRetranslateFile] = useState<VideoFile | null>(null);
  const [expandedFileIds, setExpandedFileIds] = useState<Set<number>>(() => new Set());

  const isTerminal = project.status === 'completed' || project.status === 'failed';
  const allFilesExpanded = files.length > 0 && files.every((file) => expandedFileIds.has(file.id));
  const infoUrl = seriesUrl(project);

  function handleToggleFileExpanded(fileId: number) {
    setExpandedFileIds((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) {
        next.delete(fileId);
      } else {
        next.add(fileId);
      }
      return next;
    });
  }

  function handleExpandFile(fileId: number) {
    setExpandedFileIds((prev) => (prev.has(fileId) ? prev : new Set(prev).add(fileId)));
  }

  function handleCollapseFile(fileId: number) {
    setExpandedFileIds((prev) => {
      if (!prev.has(fileId)) return prev;
      const next = new Set(prev);
      next.delete(fileId);
      return next;
    });
  }

  function handleToggleAllFilesExpanded() {
    setExpandedFileIds(() => (
      allFilesExpanded ? new Set() : new Set(files.map((file) => file.id))
    ));
  }

  async function handleDelete() {
    await deleteMutation.mutateAsync(project.id);
    setConfirmOpen(false);
    onDeleted();
  }

  async function handlePauseResume() {
    if (project.is_paused) {
      await resumeMutation.mutateAsync(project.id);
    } else {
      await pauseMutation.mutateAsync(project.id);
    }
  }

  return (
    <>
      <Modal
        opened={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Remove project"
        size="sm"
      >
        <Text size="sm" mb="lg">
          Remove <strong>{project.name}</strong>? All project data (files, jobs, characters) will be
          deleted from the database. Files on disk will not be touched.
        </Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={() => setConfirmOpen(false)}>
            Cancel
          </Button>
          <Button color="red" loading={deleteMutation.isPending} onClick={handleDelete}>
            Remove
          </Button>
        </Group>
      </Modal>

      <Modal
        opened={retranslateFile !== null}
        onClose={() => setRetranslateFile(null)}
        title="Retranslate file"
        size="sm"
      >
        <Text size="sm" mb="sm">
          Retranslate <strong>{retranslateFile?.filename}</strong> from the beginning?
        </Text>
        <Text size="sm" c="dimmed" mb="lg">
          All translated text and review issues for this file will be cleared. Existing usage and cost
          metrics are retained, so the new translation adds to the total price.
        </Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={() => setRetranslateFile(null)}>
            Cancel
          </Button>
          <Button
            color="orange"
            loading={retranslateMutation.isPending}
            onClick={async () => {
              if (!retranslateFile) return;
              const fileId = retranslateFile.id;
              try {
                await retranslateMutation.mutateAsync(fileId);
                handleExpandFile(fileId);
                setRetranslateFile(null);
              } catch {
                notifications.show({
                  color: 'red',
                  message: 'Could not restart translation for this file.',
                });
              }
            }}
          >
            Retranslate
          </Button>
        </Group>
      </Modal>

      <WatchedWordsDialog
        projectId={project.id}
        opened={watchedWordsOpen}
        onClose={() => setWatchedWordsOpen(false)}
      />

      <StyleGuideDialog
        projectId={project.id}
        opened={styleGuideOpen}
        onClose={() => setStyleGuideOpen(false)}
        initialTab={styleGuideTab}
      />

      <ReviewQueueDialog
        projectId={project.id}
        opened={reviewQueueOpen}
        onClose={() => setReviewQueueOpen(false)}
      />

      <MetricsDialog
        projectId={project.id}
        opened={metricsOpen}
        onClose={() => setMetricsOpen(false)}
      />

      <SubtitleEditorDialog
        projectId={project.id}
        file={subtitleEditorFile}
        opened={subtitleEditorFile !== null}
        onClose={() => setSubtitleEditorFile(null)}
      />

      <Stack gap="lg">
        {/* Project header */}
        <Box>
          <Group justify="space-between" align="flex-start">
            <Box>
              <Group gap="xs" align="center">
                <Title order={3}>{project.name}</Title>
                {infoUrl && (
                  <ActionIcon
                    component="a"
                    href={infoUrl}
                    target="_blank"
                    rel="noreferrer"
                    variant="subtle"
                    color="gray"
                    size="sm"
                    aria-label="Open series page"
                    title={`Open ${project.anime_provider} series page`}
                  >
                    <Info size={16} />
                  </ActionIcon>
                )}
              </Group>
              <Group gap="sm" mt={4}>
                <Badge
                  color={PROJECT_STATUS_COLORS[project.status] ?? 'gray'}
                  variant="light"
                  size="sm"
                >
                  {project.status.replace(/_/g, ' ')}
                </Badge>
                {project.is_paused && (
                  <Badge variant="outline" color="yellow" size="sm">paused</Badge>
                )}
                <Text size="xs" c="dimmed">
                  {project.source_directory}
                </Text>
              </Group>
            </Box>
            <Group gap="xs">
              <Button
                variant="subtle"
                color="teal"
                size="xs"
                leftSection={<ChartLine size={14} />}
                onClick={() => setMetricsOpen(true)}
              >
                Metrics
              </Button>
              <Button
                variant="subtle"
                color="orange"
                size="xs"
                leftSection={<ListChecks size={14} />}
                onClick={() => setReviewQueueOpen(true)}
              >
                Review queue
              </Button>
              <Button
                variant="subtle"
                color="grape"
                size="xs"
                leftSection={<BookOpen size={14} />}
                onClick={() => { setStyleGuideTab('glossary'); setStyleGuideOpen(true); }}
              >
                Style guide
              </Button>
              <Button
                variant="subtle"
                color="blue"
                size="xs"
                leftSection={<Eye size={14} />}
                onClick={() => setWatchedWordsOpen(true)}
              >
                Watched words
              </Button>
              <Tooltip label={`Re-fetch characters and episode titles from ${project.anime_provider}`} withArrow>
                <Button
                  variant="subtle"
                  color="cyan"
                  size="xs"
                  leftSection={<ArrowClockwiseIcon size={14} />}
                  loading={refreshMetadata.isPending}
                  onClick={() => {
                    refreshMetadata.mutate(project.id, {
                      onSuccess: (r) => notifications.show({
                        color: 'green',
                        message: `Metadata refreshed — ${r.characters_created + r.characters_updated} character and ${r.episodes_created + r.episodes_updated} episode change(s).`,
                      }),
                      onError: () => notifications.show({
                        color: 'red',
                        message: 'Metadata refresh failed (provider unreachable?).',
                      }),
                    });
                  }}
                >
                  Refresh metadata
                </Button>
              </Tooltip>
              {!isTerminal && (
                <Button
                  variant="subtle"
                  color={project.is_paused ? 'green' : 'yellow'}
                  size="xs"
                  leftSection={project.is_paused ? <Play size={14} /> : <Pause size={14} />}
                  loading={pauseMutation.isPending || resumeMutation.isPending}
                  onClick={handlePauseResume}
                >
                  {project.is_paused ? 'Resume' : 'Pause'}
                </Button>
              )}
              <Button
                variant="subtle"
                color="red"
                size="xs"
                leftSection={<Trash size={14} />}
                onClick={() => setConfirmOpen(true)}
              >
                Remove
              </Button>
            </Group>
          </Group>
        </Box>

      {/* Pipeline */}
      <ProjectPipeline project={project} files={files} />

      {/* Translation-context gate (hidden once approved) */}
      <ContextReviewPanel
        project={project}
        onReviewCharacters={() => { setStyleGuideTab('characters'); setStyleGuideOpen(true); }}
      />

      {/* File list */}
      <Box>
        {isLoading ? (
          <Center py="md"><Loader size="sm" /></Center>
        ) : files.length === 0 ? (
          <Text size="sm" c="dimmed">No files discovered yet.</Text>
        ) : (
          <Table striped highlightOnHover style={{ minWidth: 1150, tableLayout: 'fixed' }}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Filename</Table.Th>
                <Table.Th style={{ width: 100 }}>Chunks</Table.Th>
                <Table.Th style={{ width: 150 }}>Status</Table.Th>
                <Table.Th style={{ width: 100 }}>Format</Table.Th>
                <Table.Th style={{ width: 160 }}>Updated</Table.Th>
                <Table.Th style={{ width: 80 }}>Issues</Table.Th>
                <Table.Th style={{ width: 224 }} />
                <Table.Th style={{ width: 28, textAlign: 'center' }}>
                  <ActionIcon
                    size="xs"
                    variant="subtle"
                    color="gray"
                    title={allFilesExpanded ? 'Collapse all chunk info' : 'Expand all chunk info'}
                    aria-label={allFilesExpanded ? 'Collapse all chunk info' : 'Expand all chunk info'}
                    onClick={handleToggleAllFilesExpanded}
                  >
                    {allFilesExpanded ? <CaretDown size={12} /> : <CaretRight size={12} />}
                  </ActionIcon>
                </Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {files.map((f) => (
                <FileRow
                  key={f.id}
                  file={f}
                  projectId={project.id}
                  contextApproved={project.context_approved_at !== null}
                  expanded={expandedFileIds.has(f.id)}
                  onEditSubtitles={setSubtitleEditorFile}
                  onToggleExpanded={handleToggleFileExpanded}
                  onExpand={handleExpandFile}
                  onCollapse={handleCollapseFile}
                  onRetranslate={setRetranslateFile}
                />
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Box>
    </Stack>
    </>
  );
}

// ─── Main layout ──────────────────────────────────────────────────────────────

export function AppLayout() {
  const { data: projects = [], isLoading } = useProjects();
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const effectiveId = selectedProjectId ?? projects[0]?.id ?? null;
  const selectedProject = projects.find((p) => p.id === effectiveId) ?? null;

  return (
    <>
      <OptionsDrawer opened={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <ImportDialog opened={importOpen} onClose={() => setImportOpen(false)} />
      <AppShell
        header={{ height: 52 }}
        navbar={{ width: 260, breakpoint: 'sm' }}
        padding="md"
      >
        {/* Header */}
        <AppShell.Header
          bg="pink"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: 'none' }}
          px="md"
        >
          <Group gap="sm">
            <Image src="/logo.png" alt="Subi neko" h={30} w="auto" />
            <Title order={4} c="white" style={{ letterSpacing: '-0.3px' }}>
              Subi neko
            </Title>
          </Group>
          <ActionIcon variant="subtle" color="white" size="lg" aria-label="Settings" onClick={() => setSettingsOpen(true)}>
            <Gear size={22} />
          </ActionIcon>
        </AppShell.Header>

        {/* Sidebar */}
        <AppShell.Navbar p="sm">
          {isLoading ? (
            <Center h="100%"><Loader size="sm" /></Center>
          ) : (
            <ProjectSidebar
              projects={projects}
              selectedId={effectiveId}
              onSelect={setSelectedProjectId}
              onImport={() => setImportOpen(true)}
            />
          )}
        </AppShell.Navbar>

        {/* Main content */}
        <AppShell.Main>
          {selectedProject ? (
            <ProjectDetails
              project={selectedProject}
              onDeleted={() => setSelectedProjectId(null)}
            />
          ) : (
            <EmptyProjectLanding isLoading={isLoading} />
          )}
        </AppShell.Main>
      </AppShell>
    </>
  );
}

