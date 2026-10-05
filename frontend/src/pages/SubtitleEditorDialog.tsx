import { GENDER_COLORS, NON_BINARY_BADGE_STYLE } from '../utils/gender';
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Center,
  Checkbox,
  Group,
  Loader,
  Modal,
  Pagination,
  ScrollArea,
  Stack,
  Table,
  Text,
  Textarea,
  Tooltip,
} from '@mantine/core';
import { useQueryClient } from '@tanstack/react-query';
import { notifications } from '@mantine/notifications';
import { ArrowCounterClockwise, CheckCircle, NotePencil, WarningCircle } from '@phosphor-icons/react';
import type { ProjectWatchedWord, QaIssue, SubtitleEventEditorRow, VideoFile } from '../types';
import {
  adjustWatchedOccurrences,
  subtitleEventsKey,
  useResolveQaIssue,
  useRevertSubtitleEvent,
  useSubtitleEvents,
  useUpdateSubtitleEvent,
} from '../hooks/useSubtitleEditor';
import { useProjectWatchedWords } from '../hooks/useProjects';
import { WatchedWordBadge } from '../components/WatchedWordBadge';
import { WATCHED_ROW_BACKGROUND } from '../utils/watchedWords';
import { SEVERITY_COLORS } from '../utils/qaSeverity';

const SEVERITY_RANK: Record<string, number> = {
  blocker: 0,
  critical: 0,
  error: 0,
  high: 0,
  warning: 1,
  medium: 1,
  info: 2,
  low: 2,
};

function sortIssues(issues: QaIssue[]) {
  return [...issues].sort((a, b) => {
    if (a.is_resolved !== b.is_resolved) return a.is_resolved ? 1 : -1;
    const ar = SEVERITY_RANK[a.severity.toLowerCase()] ?? 99;
    const br = SEVERITY_RANK[b.severity.toLowerCase()] ?? 99;
    if (ar !== br) return ar - br;
    return a.id - b.id;
  });
}

// Blockers are always shown; warnings/info and resolved issues are togglable.
function filterIssues(issues: QaIssue[], showWarnings: boolean, showResolved: boolean) {
  return issues.filter((issue) => {
    if (!showResolved && issue.is_resolved) return false;
    if (!showWarnings && (SEVERITY_RANK[issue.severity.toLowerCase()] ?? 99) > 0) return false;
    return true;
  });
}

function matchingWatchedWords(text: string | null | undefined, words: ProjectWatchedWord[]) {
  const haystack = (text ?? '').toLocaleLowerCase();
  if (!haystack) return [];
  return words.filter((word) => haystack.includes(word.word.toLocaleLowerCase()));
}

function WatchedWordBadges({ words }: { words: ProjectWatchedWord[] }) {
  if (words.length === 0) return null;
  return (
    <Group gap={4} mt={5}>
      {words.map((word) => (
        <WatchedWordBadge key={word.id} word={word.word} />
      ))}
    </Group>
  );
}

function IdentityBadges({
  name,
  gender,
}: {
  name: string;
  gender: string | null;
}) {
  return (
    <Group gap={4} mt={8} wrap="nowrap" style={{ minWidth: 0 }}>
      <Badge size="xs" variant="light" color="blue" style={{ maxWidth: 112, minWidth: 0 }}>
        <Text size="xs" truncate title={name}>{name}</Text>
      </Badge>
      {gender && (
        <Badge
          size="xs"
          variant="outline"
          color={GENDER_COLORS[gender] ?? 'gray'}
          style={{
            flexShrink: 0,
            ...(gender === 'non_binary' ? NON_BINARY_BADGE_STYLE : {}),
          }}
        >
          {gender}
        </Badge>
      )}
    </Group>
  );
}

function IssueRow({
  issue,
  onResolve,
  resolving,
}: {
  issue: QaIssue;
  onResolve: (issueId: number) => void | Promise<void>;
  resolving: boolean;
}) {
  const color = SEVERITY_COLORS[issue.severity.toLowerCase()] ?? 'gray';

  return (
    <Group
      gap="xs"
      wrap="nowrap"
      align="flex-start"
      px="xs"
      py={6}
      style={{
        border: '1px solid var(--mantine-color-dark-5)',
        borderRadius: 6,
        backgroundColor: 'var(--mantine-color-dark-7)',
        opacity: issue.is_resolved ? 0.55 : 1,
      }}
    >
      <Badge size="xs" color={color} variant="light" style={{ width: 64, flexShrink: 0 }}>
        {issue.severity}
      </Badge>
      <Box style={{ flex: 1, minWidth: 0 }}>
        <Group gap={6} wrap="nowrap" mb={2}>
          <Text size="xs" fw={600} truncate>
            {issue.qa_type.replace(/_/g, ' ')}
          </Text>
        </Group>
        <Text size="xs" c="dimmed" style={{ whiteSpace: 'normal' }}>
          {issue.message}
        </Text>
      </Box>
      {issue.is_resolved ? (
        <Badge
          size="xs"
          color="green"
          variant="outline"
          title={issue.resolution_note ? `Resolved: ${issue.resolution_note}` : 'Resolved'}
          style={{ flexShrink: 0 }}
        >
          resolved
        </Badge>
      ) : (
        <Tooltip label="Resolve issue" withArrow>
          <Button
            size="compact-xs"
            variant="subtle"
            color="green"
            loading={resolving}
            leftSection={<CheckCircle size={13} />}
            onClick={() => onResolve(issue.id)}
            style={{ flexShrink: 0 }}
          >
            Solve
          </Button>
        </Tooltip>
      )}
    </Group>
  );
}

interface SubtitleRowProps {
  row: SubtitleEventEditorRow;
  cpsLimit: number;
  originalWatchedWords: ProjectWatchedWord[];
  translatedWatchedWords: ProjectWatchedWord[];
  showInfo: boolean;
  showResolved: boolean;
  /** Persist an edit; resolves to the authoritative row, or null on failure. */
  onSave: (row: SubtitleEventEditorRow, text: string) => Promise<SubtitleEventEditorRow | null>;
  onRevert: (row: SubtitleEventEditorRow) => Promise<SubtitleEventEditorRow | null>;
  onResolve: (issueId: number) => Promise<void>;
  onDirtyChange: (eventId: number, dirty: boolean) => void;
}

// Every prop is referentially stable between unrelated renders (callbacks are
// useCallback'd in the dialog, word lists are memoized, `row` only changes
// when the server row does), so plain shallow memo is enough: typing in one
// row re-renders that row only.
const SubtitleRow = memo(function SubtitleRow({
  row,
  cpsLimit,
  originalWatchedWords,
  translatedWatchedWords,
  showInfo,
  showResolved,
  onSave,
  onRevert,
  onResolve,
  onDirtyChange,
}: SubtitleRowProps) {
  const serverText = row.translated_text ?? '';
  // The draft lives here, not in the dialog: keystrokes touch this row only.
  const [text, setText] = useState(serverText);
  const [seenServerText, setSeenServerText] = useState(serverText);
  const [saving, setSaving] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [resolvingIssueId, setResolvingIssueId] = useState<number | null>(null);

  // Server row changed (save echo, revert, refetch): follow it unless the
  // user has an unsaved draft on top of the previous server text.
  if (serverText !== seenServerText) {
    setSeenServerText(serverText);
    if (text === seenServerText) setText(serverText);
  }

  const dirty = text !== serverText;
  useEffect(() => {
    onDirtyChange(row.id, dirty);
  }, [dirty, onDirtyChange, row.id]);
  useEffect(() => () => onDirtyChange(row.id, false), [onDirtyChange, row.id]);

  const issues = useMemo(
    () => sortIssues(filterIssues(row.issues, showInfo, showResolved)),
    [row.issues, showInfo, showResolved],
  );
  const canRevert = row.original_ai_translated_text !== null
    && text !== row.original_ai_translated_text;
  const originalMatches = useMemo(
    () => matchingWatchedWords(row.source_text, originalWatchedWords),
    [row.source_text, originalWatchedWords],
  );
  const translatedMatches = useMemo(
    () => matchingWatchedWords(text, translatedWatchedWords),
    [text, translatedWatchedWords],
  );
  const hasWatchedMatch = originalMatches.length > 0 || translatedMatches.length > 0;
  const identityName = row.character_name ?? row.speaker_name;
  const identityGender = row.character_name ? row.character_gender : row.speaker_gender;

  const handleBlur = async () => {
    if (saving || text === serverText) return;
    setSaving(true);
    try {
      const saved = await onSave(row, text);
      if (saved) setText(saved.translated_text ?? '');
    } finally {
      setSaving(false);
    }
  };

  const handleRevert = async () => {
    setReverting(true);
    try {
      const reverted = await onRevert(row);
      if (reverted) setText(reverted.translated_text ?? '');
    } finally {
      setReverting(false);
    }
  };

  const handleResolve = async (issueId: number) => {
    setResolvingIssueId(issueId);
    try {
      await onResolve(issueId);
    } finally {
      setResolvingIssueId(null);
    }
  };

  const cpsOver = row.cps !== null && row.cps > cpsLimit;

  return (
    <Table.Tr
      style={{
        backgroundColor: hasWatchedMatch
          ? WATCHED_ROW_BACKGROUND
          : dirty ? 'var(--mantine-color-dark-6)' : undefined,
      }}
    >
      <Table.Td style={{ width: 160, verticalAlign: 'top' }}>
        <Group gap={6} wrap="nowrap" align="center">
          <Text size="sm" fw={700}>{row.line_index + 1}</Text>
          {dirty && (
            <Tooltip label="Unsaved changes" withArrow>
              <Box style={{ width: 7, height: 7, borderRadius: '50%', backgroundColor: 'var(--mantine-color-orange-5)', flexShrink: 0 }} />
            </Tooltip>
          )}
        </Group>
        {identityName && (
          <IdentityBadges name={identityName} gender={identityGender} />
        )}
      </Table.Td>
      <Table.Td style={{ width: '28%', verticalAlign: 'top' }}>
        <Textarea
          autosize
          minRows={2}
          maxRows={8}
          value={row.source_text}
          readOnly
          styles={{ input: { fontSize: 13, lineHeight: 1.35 } }}
        />
        <WatchedWordBadges words={originalMatches} />
      </Table.Td>
      <Table.Td style={{ width: '28%', verticalAlign: 'top' }}>
        <Group gap={6} align="flex-start" wrap="nowrap">
          <Textarea
            autosize
            minRows={2}
            maxRows={8}
            value={text}
            onChange={(e) => setText(e.currentTarget.value)}
            onBlur={handleBlur}
            // readOnly (not disabled) while saving: a disabled textarea drops
            // focus, which moved the caret away mid-save.
            readOnly={saving || reverting}
            styles={{ root: { flex: 1 }, input: { fontSize: 13, lineHeight: 1.35 } }}
          />
          <Tooltip label="Revert to AI translation" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              aria-label="Revert to AI translation"
              disabled={!canRevert || saving || reverting}
              loading={reverting}
              onMouseDown={(e) => e.preventDefault()}
              onClick={handleRevert}
            >
              <ArrowCounterClockwise size={15} />
            </ActionIcon>
          </Tooltip>
        </Group>
        <WatchedWordBadges words={translatedMatches} />
        {saving && <Text size="xs" c="dimmed" mt={4}>Saving…</Text>}
      </Table.Td>
      <Table.Td style={{ width: 64, verticalAlign: 'top', textAlign: 'center' }}>
        <Tooltip
          label="Raw reading speed; QA may allow longer translations based on source length."
          withArrow
          multiline
          w={220}
        >
          <Text
            size="sm"
            fw={cpsOver ? 700 : 400}
            c={cpsOver ? 'red' : 'dimmed'}
            data-testid="cps-cell"
            data-cps-over={cpsOver ? 'true' : 'false'}
            style={{ paddingTop: 8 }}
          >
            {row.cps === null ? '—' : Math.round(row.cps)}
          </Text>
        </Tooltip>
      </Table.Td>
      <Table.Td style={{ verticalAlign: 'top' }}>
        {issues.length === 0 ? (
          <Text size="xs" c="dimmed">No issues</Text>
        ) : (
          <Stack gap={6}>
            {issues.map((issue) => (
              <IssueRow
                key={issue.id}
                issue={issue}
                resolving={resolvingIssueId === issue.id}
                onResolve={handleResolve}
              />
            ))}
          </Stack>
        )}
      </Table.Td>
    </Table.Tr>
  );
});

interface SubtitleEditorDialogProps {
  projectId: number;
  file: VideoFile | null;
  opened: boolean;
  onClose: () => void;
}

export function SubtitleEditorDialog({ projectId, file, opened, onClose }: SubtitleEditorDialogProps) {
  const fileId = file?.id ?? null;
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [issuesOnly, setIssuesOnly] = useState(false);
  const [showInfo, setShowInfo] = useState(true);
  const [showResolved, setShowResolved] = useState(false);
  const filters = useMemo(
    () => ({ showInfo, showResolved, issuesOnly }),
    [showInfo, showResolved, issuesOnly],
  );
  const { data, isLoading, isPlaceholderData } = useSubtitleEvents(
    projectId, fileId, opened, page, filters);
  const { data: watchedWords = [] } = useProjectWatchedWords(projectId, opened);
  const { mutateAsync: updateEvent, isPending: isUpdating } = useUpdateSubtitleEvent();
  const { mutateAsync: revertEvent, isPending: isReverting } = useRevertSubtitleEvent();
  const { mutateAsync: resolveIssue, isPending: isResolving } = useResolveQaIssue();
  const scrollViewport = useRef<HTMLDivElement>(null);

  // Dirty rows register themselves; the Set lives in a ref (no re-render per
  // keystroke) and only the count — which changes on dirty flips — is state.
  const dirtyIdsRef = useRef<Set<number>>(new Set());
  const [dirtyCount, setDirtyCount] = useState(0);
  const pendingSavesRef = useRef<Set<Promise<unknown>>>(new Set());
  const discardArmedRef = useRef(false);

  const items = data?.items;
  const summary = data?.summary;
  const rows = items ?? [];

  // The server clamps an out-of-range page; follow it.
  if (data && !isPlaceholderData && data.page !== page) setPage(data.page);

  // Back to the top of the list on every page/filter change.
  useEffect(() => {
    scrollViewport.current?.scrollTo({ top: 0 });
  }, [page, issuesOnly, showInfo, showResolved]);

  const watchedWordsByType = useMemo(() => ({
    original: watchedWords.filter((word) => word.word_type === 'original'),
    translated: watchedWords.filter((word) => word.word_type === 'translated'),
  }), [watchedWords]);
  const watchedRef = useRef(watchedWordsByType);
  useEffect(() => {
    watchedRef.current = watchedWordsByType;
  }, [watchedWordsByType]);

  const issueSummary = useMemo(
    () => [...(summary?.issue_counts ?? [])].sort((a, b) => {
      const ar = SEVERITY_RANK[a.severity.toLowerCase()] ?? 99;
      const br = SEVERITY_RANK[b.severity.toLowerCase()] ?? 99;
      if (ar !== br) return ar - br;
      return b.count - a.count;
    }),
    [summary?.issue_counts],
  );
  const isBusy = isUpdating || isReverting || isResolving;

  const handleDirtyChange = useCallback((eventId: number, dirty: boolean) => {
    const ids = dirtyIdsRef.current;
    if (dirty === ids.has(eventId)) return;
    if (dirty) ids.add(eventId); else ids.delete(eventId);
    setDirtyCount(ids.size);
  }, []);

  // Same matcher the rows use for their badges, applied to the change in one
  // event's saved text, keeps the header total in step without a refetch.
  const adjustWatched = useCallback((before: string | null, after: string | null) => {
    if (fileId === null) return;
    const words = watchedRef.current.translated;
    const delta = matchingWatchedWords(after, words).length - matchingWatchedWords(before, words).length;
    adjustWatchedOccurrences(queryClient, projectId, fileId, delta);
  }, [fileId, projectId, queryClient]);

  const handleSave = useCallback((row: SubtitleEventEditorRow, text: string) => {
    if (fileId === null) return Promise.resolve(null);
    const task = (async () => {
      try {
        const saved = await updateEvent({
          projectId,
          fileId,
          eventId: row.id,
          translated_text: text || null,
        });
        adjustWatched(row.translated_text, saved.translated_text);
        handleDirtyChange(row.id, false);
        return saved;
      } catch {
        notifications.show({ color: 'red', title: 'Save failed', message: 'Could not save subtitle edits.' });
        return null;
      }
    })();
    pendingSavesRef.current.add(task);
    void task.finally(() => pendingSavesRef.current.delete(task));
    return task;
  }, [adjustWatched, fileId, handleDirtyChange, projectId, updateEvent]);

  const handleRevert = useCallback(async (row: SubtitleEventEditorRow) => {
    if (fileId === null) return null;
    try {
      const reverted = await revertEvent({ projectId, fileId, eventId: row.id });
      adjustWatched(row.translated_text, reverted.translated_text);
      handleDirtyChange(row.id, false);
      return reverted;
    } catch {
      notifications.show({ color: 'red', title: 'Revert failed', message: 'Could not restore the original AI translation.' });
      return null;
    }
  }, [adjustWatched, fileId, handleDirtyChange, projectId, revertEvent]);

  const handleResolve = useCallback(async (issueId: number) => {
    if (fileId === null) return;
    try {
      await resolveIssue({ projectId, fileId, issueId });
    } catch {
      notifications.show({ color: 'red', title: 'Resolve failed', message: 'Could not resolve the QA issue.' });
    }
  }, [fileId, projectId, resolveIssue]);

  // Blur whatever is focused (its onBlur starts the save), wait for every
  // save in flight, and report whether any edit is still unsaved.
  const flushEdits = useCallback(async () => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    await Promise.allSettled([...pendingSavesRef.current]);
    return dirtyIdsRef.current.size === 0;
  }, []);

  // Anything that remounts the rows (page, filters) must not drop an edit.
  const guarded = useCallback(async (action: () => void) => {
    if (await flushEdits()) {
      action();
    } else {
      notifications.show({
        color: 'orange',
        title: 'Unsaved edits',
        message: 'Some edits could not be saved yet — fix or retry before changing the view.',
      });
    }
  }, [flushEdits]);

  const goToPage = (next: number) => { void guarded(() => setPage(next)); };
  const changeFilter = (apply: () => void) => {
    void guarded(() => {
      apply();
      setPage(1);
    });
  };

  async function handleClose() {
    const clean = await flushEdits();
    if (!clean && !discardArmedRef.current) {
      // Keep the modal open once so a failed save isn't silently lost.
      discardArmedRef.current = true;
      notifications.show({
        color: 'orange',
        title: 'Unsaved edits',
        message: 'Some edits could not be saved. Close again to discard them.',
      });
      return;
    }
    discardArmedRef.current = false;
    dirtyIdsRef.current.clear();
    setDirtyCount(0);
    setPage(1);
    setIssuesOnly(false);
    setShowInfo(true);
    setShowResolved(false);
    if (fileId !== null) {
      // Pages are only a snapshot; reopening must start from the server.
      queryClient.removeQueries({ queryKey: subtitleEventsKey(projectId, fileId) });
    }
    onClose();
  }

  const totalEvents = summary?.total_events ?? 0;
  const filteredEvents = data?.filtered_events ?? 0;
  const totalPages = data?.total_pages ?? 1;
  const rangeStart = rows.length > 0 && data ? (data.page - 1) * data.page_size + 1 : 0;
  const rangeEnd = rows.length > 0 ? rangeStart + rows.length - 1 : 0;
  const unresolvedCount = summary?.unresolved_issue_count ?? 0;
  const cpsLimit = summary?.cps_limit ?? Number.POSITIVE_INFINITY;

  return (
    <Modal
      opened={opened}
      onClose={() => { void handleClose(); }}
      title={
        <Group gap="xs">
          <NotePencil size={18} />
          <Text fw={600}>Subtitle Editor</Text>
          {file && <Text size="sm" c="dimmed">{file.filename}</Text>}
          {dirtyCount > 0 && <Badge size="sm" color="orange" variant="light">{dirtyCount} unsaved</Badge>}
          {unresolvedCount > 0 && <Badge size="sm" color="red" variant="light">{unresolvedCount} issues</Badge>}
          {watchedWords.length > 0 && summary && (
            <Badge
              size="sm"
              color="yellow"
              variant="light"
              title="Watched-word matches across the whole file"
            >
              Watched {summary.watched_occurrences}
            </Badge>
          )}
        </Group>
      }
      size="95%"
      styles={{
        body: { padding: 'var(--mantine-spacing-md)' },
        content: { display: 'flex', flexDirection: 'column' },
        inner: { padding: '2vh 2vw' },
      }}
    >
      {isLoading || !data || !summary ? (
        <Center py="xl"><Loader size="sm" /></Center>
      ) : totalEvents === 0 ? (
        <Center py="xl">
          <Group gap="xs">
            <WarningCircle size={16} />
            <Text size="sm" c="dimmed">No subtitle events found for this file.</Text>
          </Group>
        </Center>
      ) : (
        <Stack gap="sm">
          <Group justify="space-between" gap="sm">
            <Group gap="xs" wrap="nowrap" style={{ minWidth: 0, flex: 1 }}>
              <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                {filteredEvents === 0
                  ? `No matching events (of ${totalEvents})`
                  : `Showing ${rangeStart}–${rangeEnd} of ${filteredEvents} events`
                    + (filteredEvents !== totalEvents ? ` (${totalEvents} in file)` : '')}
              </Text>
              {issueSummary.length > 0 && (
                <Group gap={4} wrap="nowrap" style={{ minWidth: 0, overflow: 'hidden' }}>
                  {issueSummary.map((item) => (
                    <Badge
                      key={`${item.severity}:${item.qa_type}`}
                      size="xs"
                      color={SEVERITY_COLORS[item.severity.toLowerCase()] ?? 'gray'}
                      variant="light"
                      title={`${item.count} ${item.qa_type.replace(/_/g, ' ')}`}
                      style={{ flexShrink: 0 }}
                    >
                      {item.qa_type.replace(/_/g, ' ')}: {item.count}
                    </Badge>
                  ))}
                </Group>
              )}
            </Group>
            <Group gap="md" wrap="nowrap" style={{ flexShrink: 0 }}>
              <Checkbox
                size="xs"
                checked={showInfo}
                label="Info"
                onChange={(e) => {
                  const checked = e.currentTarget.checked;
                  changeFilter(() => setShowInfo(checked));
                }}
              />
              <Checkbox
                size="xs"
                checked={showResolved}
                label="Resolved issues"
                onChange={(e) => {
                  const checked = e.currentTarget.checked;
                  changeFilter(() => setShowResolved(checked));
                }}
              />
              <Checkbox
                size="xs"
                checked={issuesOnly}
                label="Show only events with issues"
                onChange={(e) => {
                  const checked = e.currentTarget.checked;
                  changeFilter(() => setIssuesOnly(checked));
                }}
              />
            </Group>
          </Group>

          {rows.length === 0 ? (
            <Center h="70vh">
              <Text size="sm" c="dimmed">No events with unresolved issues.</Text>
            </Center>
          ) : (
            <ScrollArea h="70vh" type="auto" viewportRef={scrollViewport}>
              <Table
                striped
                highlightOnHover
                withColumnBorders
                style={{ tableLayout: 'fixed', opacity: isPlaceholderData ? 0.6 : 1 }}
              >
                <Table.Thead style={{ position: 'sticky', top: 0, zIndex: 1, backgroundColor: 'var(--mantine-color-dark-7)' }}>
                  <Table.Tr>
                    <Table.Th style={{ width: 160 }}>Event</Table.Th>
                    <Table.Th style={{ width: '28%' }}>English text</Table.Th>
                    <Table.Th style={{ width: '28%' }}>Translation</Table.Th>
                    <Table.Th style={{ width: 64, textAlign: 'center' }}>CPS</Table.Th>
                    <Table.Th>Issues</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {rows.map((row) => (
                    <SubtitleRow
                      key={row.id}
                      row={row}
                      cpsLimit={cpsLimit}
                      originalWatchedWords={watchedWordsByType.original}
                      translatedWatchedWords={watchedWordsByType.translated}
                      showInfo={showInfo}
                      showResolved={showResolved}
                      onSave={handleSave}
                      onRevert={handleRevert}
                      onResolve={handleResolve}
                      onDirtyChange={handleDirtyChange}
                    />
                  ))}
                </Table.Tbody>
              </Table>
            </ScrollArea>
          )}

          {totalPages > 1 && (
            <Group justify="space-between" gap="sm">
              <Text size="xs" c="dimmed">Page {data.page} of {totalPages}</Text>
              <Pagination
                size="xs"
                total={totalPages}
                value={data.page}
                onChange={goToPage}
                withEdges
              />
            </Group>
          )}
        </Stack>
      )}

      {isBusy && (
        <Box pt="md" style={{ flexShrink: 0 }}>
          <Text size="xs" c="dimmed">Saving changes…</Text>
        </Box>
      )}
    </Modal>
  );
}
