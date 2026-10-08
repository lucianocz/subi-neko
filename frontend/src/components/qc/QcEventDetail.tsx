import { useState } from 'react';
import {
  ActionIcon, Alert, Badge, Box, Button, Group, Input, Loader, NativeSelect, Stack, Text, Textarea, TextInput, Tooltip,
} from '@mantine/core';
import type { QcEvent, QcEventDetail as QcEventDetailDto } from '../../types/qc';
import type { EventDraft, NewEventDraft } from '../../utils/qcDraft';
import { validateNewEvent } from '../../utils/qcDraft';
import { ArrowCounterClockwise, CheckCircle, Info, Warning, XCircle } from '@phosphor-icons/react';
import type { Icon } from '@phosphor-icons/react';
import { CPS_COLORS, cpsSeverity } from '../../utils/cps';
import type { CpsLimits } from '../../utils/cps';
import { canRestoreAi } from '../../utils/qcQa';
import { SEVERITY_COLORS } from '../../utils/qaSeverity';
import { formatMs } from '../../utils/qcText';
import { setBoundary } from '../../utils/qcTime';
import type { Timing } from '../../utils/qcTime';
import { formatGap, identityLabel, neighbourGaps, unresolvedSeverityCounts } from '../../utils/qcDetail';
import type { SeverityBucket } from '../../utils/qcDetail';
import { WatchedWordBadge } from '../WatchedWordBadge';
import { GENDER_COLORS, NON_BINARY_BADGE_STYLE } from '../../utils/gender';
import { QcTimeInput } from './QcTimeInput';

const SEVERITY_ICONS: Record<SeverityBucket, Icon> = { blocker: XCircle, warning: Warning, info: Info };
const SEVERITY_LABELS: Record<SeverityBucket, string> = { blocker: 'Blocker', warning: 'Warning', info: 'Info' };

/** Raw source text in a read-only (selectable, copyable) textarea — never `disabled`. */
function SourceBlock({ text, matches }: { text: string | null; matches: string[] }) {
  return (
    <Box>
      <Textarea
        className="qc-detail-text qc-source-text"
        label="Source"
        size="xs"
        readOnly
        autosize
        minRows={2}
        maxRows={8}
        placeholder="(empty)"
        data-testid="qc-source"
        value={text ?? ''}
      />
      {matches.length > 0 && (
        <Group gap={4} mt={4}>{matches.map((w) => <WatchedWordBadge key={w} word={w} />)}</Group>
      )}
    </Box>
  );
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="qc-meta-row">
      <Text size="xs" c="dimmed">{label}</Text>
      <Text size="sm" component="div" className="qc-meta-value">{children}</Text>
    </div>
  );
}

function GenderBadge({ gender }: { gender: string | null }) {
  if (!gender) return <Badge size="sm" variant="outline" color="gray" data-testid="qc-gender">Not set</Badge>;
  return (
    <Badge
      size="sm"
      variant="outline"
      color={GENDER_COLORS[gender] ?? 'gray'}
      style={gender === 'non_binary' ? NON_BINARY_BADGE_STYLE : undefined}
      data-testid="qc-gender"
    >
      {gender}
    </Badge>
  );
}

/** Compact read-only facts about the selected event (top of the right panel). */
function QcEventMetadata({
  event, d, loading,
}: {
  event: QcEvent;
  d: QcEventDetailDto | undefined;
  loading: boolean;
}) {
  const timingChanged = d && (d.original_start_ms !== event.start_ms || d.original_end_ms !== event.end_ms);
  // Same identity rule as the subtitle editor: mapped character's gender, else the speaker's.
  const gender = d ? (d.character_name ? d.character_gender : d.speaker_gender) : null;
  return (
    <div className="qc-meta-body" data-testid="qc-meta">
      <Group gap={6} wrap="wrap" className="qc-meta-who">
        <MetaRow label="Character">
          <Group gap={8}>
          <Text span size="sm" fw={600} data-testid="qc-character">
            {d ? identityLabel(d.character_name, event.speaker) : loading ? '…' : identityLabel(null, event.speaker)}
          </Text>
          {d && <GenderBadge gender={gender} />}
          </Group>
        </MetaRow>
      </Group>
      <MetaRow label="Style">{event.style}</MetaRow>
      {d && (
        <MetaRow label="Original">
          <code data-testid="qc-original-timing">{formatMs(d.original_start_ms)} → {formatMs(d.original_end_ms)}</code>
          {timingChanged && <Text span size="xs" c="orange" ml={4}>(changed)</Text>}
        </MetaRow>
      )}
    </div>
  );
}

/** CPS next to Duration: same label-over-value structure, bold threshold-coloured number. */
function CpsReadout({ cps, limits }: { cps: number | null; limits: CpsLimits }) {
  const level = cpsSeverity(cps, limits);
  return (
    <Input.Wrapper
      label="CPS"
      size="xs"
      styles={{ root: { flex: '0 0 auto' } }}
    >
      <div className="qc-cps-value">
        {cps == null ? <Text span size="md" c="dimmed">—</Text> : (
          <Text
            span size="lg" fw={700} c={CPS_COLORS[level]}
            data-testid="qc-cps" data-cps-over={level === 'error' ? 'true' : 'false'} data-cps-level={level}
          >
            {cps.toFixed(1)}
          </Text>
        )}
      </div>
    </Input.Wrapper>
  );
}

/** Every QA issue of the event, resolved ones included (bottom of the right panel). */
function QcIssuesPanel({
  d, event, loading, onResolveIssue, resolvingIssueId,
}: {
  d: QcEventDetailDto | undefined;
  event: QcEvent;
  loading: boolean;
  onResolveIssue: (issueId: number) => void;
  resolvingIssueId: number | null;
}) {
  if (!d) {
    return (
      <Text size="xs" c="dimmed" data-testid="qc-qa-heading">
        QA issues{loading ? ' …' : event.issue_count > 0 ? ` (${event.issue_count} unresolved)` : ''}
      </Text>
    );
  }
  if (d.issues.length === 0) return <Text size="xs" c="dimmed" data-testid="qc-qa-heading">QA issues: none</Text>;
  const unresolved = d.issues.filter((i) => !i.is_resolved).length;
  return (
    <>
      <Text size="xs" c="dimmed" mb={2} data-testid="qc-qa-heading">
        QA issues ({d.issues.length}, {unresolved} unresolved)
      </Text>
      <div className="qc-issues-scroll" data-testid="qc-issues-list">
        {d.issues.map((issue) => (
          <div
            key={issue.id} className="qc-issue"
            data-testid="qc-issue" data-resolved={issue.is_resolved ? 'true' : 'false'}
            style={{ opacity: issue.is_resolved ? 0.55 : 1 }}
          >
            <Group gap={6} wrap="nowrap" justify="space-between">
              <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                <Badge size="xs" variant="light" style={{ flexShrink: 0 }} color={SEVERITY_COLORS[issue.severity.toLowerCase()] ?? 'gray'}>{issue.severity}</Badge>
                <Text size="xs" c="dimmed" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{issue.qa_type}</Text>
              </Group>
              {issue.is_resolved ? (
                <Badge
                  size="xs" color="green" variant="outline" style={{ flexShrink: 0 }}
                  title={issue.resolution_note ? `Resolved: ${issue.resolution_note}` : 'Resolved'}
                >
                  resolved
                </Badge>
              ) : (
                <Button
                  size="compact-xs" variant="subtle" color="green" style={{ flexShrink: 0 }}
                  data-testid="qc-resolve-issue"
                  title="Resolve issue"
                  leftSection={<CheckCircle size={13} />}
                  loading={resolvingIssueId === issue.id}
                  disabled={resolvingIssueId !== null && resolvingIssueId !== issue.id}
                  onClick={() => onResolveIssue(issue.id)}
                >
                  Resolve
                </Button>
              )}
            </Group>
            <Text size="sm" className="qc-issue-text" data-testid="qc-issue-text">{issue.message}</Text>
          </div>
        ))}
      </div>
    </>
  );
}

/** One icon + plain count per severity that still has unresolved issues. */
function SeverityIndicators({ issues }: { issues: readonly { severity: string; is_resolved: boolean }[] }) {
  const groups = unresolvedSeverityCounts(issues);
  if (groups.length === 0) return null;
  return (
    <Group gap={8} wrap="nowrap" data-testid="qc-severity-indicators">
      {groups.map(({ severity, count }) => {
        const SeverityIcon = SEVERITY_ICONS[severity];
        const label = `${SEVERITY_LABELS[severity]}: ${count} unresolved`;
        return (
          <Tooltip key={severity} label={label} openDelay={200}>
            <Group
              gap={2} wrap="nowrap" aria-label={label} data-testid={`qc-severity-${severity}`} data-count={count}
              style={{ color: `var(--mantine-color-${SEVERITY_COLORS[severity]}-filled)` }}
            >
              <SeverityIcon size={14} weight="fill" />
              <Text span size="xs" fw={600} c="inherit">{count}</Text>
            </Group>
          </Tooltip>
        );
      })}
    </Group>
  );
}

function TimingEditor({
  timing, onChange, onBlurred, getVideoTimeMs, prefix, previousGapMs, nextGapMs, cps, extra,
}: {
  timing: Timing;
  onChange: (t: Timing) => void;
  onBlurred: () => void;
  getVideoTimeMs: () => number | null;
  prefix: string;
  /** Gap descriptions under Start / End; leave undefined for none (a not-yet-placed event). */
  previousGapMs?: number | null;
  nextGapMs?: number | null;
  /** Rendered after Duration (the CPS readout). */
  cps?: React.ReactNode;
  /** Right-aligned action below the timing row. */
  extra?: React.ReactNode;
}) {
  const apply = (result: ReturnType<typeof setBoundary>): string | null => {
    if (!result.ok) return result.reason;
    onChange(result.timing);
    return null;
  };
  const gap = (kind: 'Previous' | 'Next', ms: number | null | undefined) => {
    if (ms === undefined) return {};
    const g = formatGap(kind, ms);
    return {
      description: <span data-testid={`${prefix}-${kind.toLowerCase()}-gap`} data-overlap={g.overlap ? 'true' : 'false'}>{g.text}</span>,
      descriptionColor: g.overlap ? 'var(--mantine-color-orange-filled)' : undefined,
    };
  };

  return (
    <Stack gap={4}>
      <Group gap="sm" align="flex-start" wrap="wrap" className="qc-timing-row">
        <QcTimeInput
          label="Start" testId={`${prefix}-start`} valueMs={timing.startMs}
          onChange={(ms) => apply(setBoundary(timing, 'start', ms))}
          onBlurred={onBlurred} getCurrentMs={getVideoTimeMs}
          {...gap('Previous', previousGapMs)}
        />
        <QcTimeInput
          label="End" testId={`${prefix}-end`} valueMs={timing.endMs}
          onChange={(ms) => apply(setBoundary(timing, 'end', ms))}
          onBlurred={onBlurred} getCurrentMs={getVideoTimeMs}
          {...gap('Next', nextGapMs)}
        />
        <Input.Wrapper label="Duration" size="xs" styles={{ root: { flex: '0 1 88px', minWidth: 76 } }}>
          <TextInput
            className="qc-duration-input" size="xs" readOnly aria-label="Duration" data-testid={`${prefix}-duration`}
            value={`${((timing.endMs - timing.startMs) / 1000).toFixed(2)} s`}
          />
        </Input.Wrapper>
        {cps}
      </Group>
      {extra && <Group justify="flex-end">{extra}</Group>}
    </Stack>
  );
}

export interface QcEventDetailProps {
  event: QcEvent | null;
  detail: QcEventDetailDto | undefined;
  loading: boolean;
  error: boolean;
  cpsLimits: CpsLimits;
  /** The loaded (chronologically sorted) event list, used for the neighbour gaps. */
  events: readonly QcEvent[];
  draft: EventDraft | null;
  onDraftChange: (draft: EventDraft) => void;
  dirty: boolean;
  saving: boolean;
  saveError: string | null;
  /** Save the pending edit now (blur / Ctrl+Enter). */
  onFlush: () => void;
  onToggleHidden: () => void;
  /** Resolve one QA issue (the legacy editor's endpoint; review state only). */
  onResolveIssue: (issueId: number) => void;
  resolvingIssueId: number | null;
  /** Put the original AI translation back (locks the event). */
  onRestoreAi: () => void;
  actionBusy: boolean;
  getVideoTimeMs: () => number | null;
  newDraft: NewEventDraft | null;
  newError: string | null;
  styles: readonly string[];
  onNewDraftChange: (draft: NewEventDraft) => void;
  onCreate: () => void;
  onCancelNew: () => void;
}

function NewEventEditor(props: QcEventDetailProps & { newDraft: NewEventDraft }) {
  const { newDraft: d, styles, onNewDraftChange, onCreate, onCancelNew, getVideoTimeMs, actionBusy, newError } = props;
  const [showErrors, setShowErrors] = useState(false);
  const errors = validateNewEvent(d, styles);
  return (
    <Stack gap="xs" data-testid="qc-new-event">
      <Group gap={6}>
        <Badge size="sm" color="teal" variant="light">New manual event</Badge>
        <Text size="xs" c="dimmed">Not saved yet — it is created when you press “Add event”.</Text>
      </Group>
      <div className="qc-detail-wrap">
        <div className="qc-detail-grid">
          <Stack gap="xs" className="qc-detail-main">
            <Text size="xs" c="dimmed" className="qc-empty-text">No source text — manual event</Text>
            <Textarea
              className="qc-detail-text"
              label="Translation"
              size="xs"
              autosize
              minRows={2}
              maxRows={8}
              data-testid="qc-new-text"
              value={d.text}
              onChange={(e) => onNewDraftChange({ ...d, text: e.currentTarget.value })}
            />
            <TimingEditor
              prefix="qc-new"
              timing={{ startMs: d.startMs, endMs: d.endMs }}
              onChange={(t) => onNewDraftChange({ ...d, startMs: t.startMs, endMs: t.endMs })}
              onBlurred={() => { /* nothing is saved until Add event */ }}
              getVideoTimeMs={getVideoTimeMs}
            />
            <Group gap="md" align="flex-start">
              <NativeSelect
                size="xs"
                label="Style"
                data-testid="qc-new-style"
                value={d.style}
                data={[{ value: '', label: '— choose style —' }, ...styles.map((s) => ({ value: s, label: s }))]}
                onChange={(e) => onNewDraftChange({ ...d, style: e.currentTarget.value })}
              />
              <TextInput
                size="xs"
                label="Speaker (optional)"
                data-testid="qc-new-speaker"
                value={d.speaker}
                onChange={(e) => onNewDraftChange({ ...d, speaker: e.currentTarget.value })}
              />
            </Group>
          </Stack>
          <div className="qc-meta">
            <MetaRow label="Status">Manual · unsaved</MetaRow>
            <MetaRow label="Style">{d.style || '—'}</MetaRow>
            <MetaRow label="Speaker">{d.speaker.trim() || '—'}</MetaRow>
          </div>
        </div>
      </div>
      {(showErrors && errors.length > 0) && (
        <Alert color="red" p="xs" data-testid="qc-new-errors">{errors.join(' ')}</Alert>
      )}
      {newError && <Alert color="red" p="xs">{newError}</Alert>}
      <Group gap="xs">
        <Button
          size="xs"
          data-testid="qc-new-add"
          loading={actionBusy}
          onClick={() => { setShowErrors(true); if (errors.length === 0) onCreate(); }}
        >
          Add event
        </Button>
        <Button size="xs" variant="default" onClick={onCancelNew} disabled={actionBusy}>Cancel</Button>
      </Group>
    </Stack>
  );
}

/**
 * Editor of the selected event (translated text + timing; everything else is
 * read-only). Edits live in a local draft owned by the page; nothing is sent while
 * typing. Instant fields come from the list row; QA issues / watched matches /
 * original timing come from the lazily fetched detail (`detail` may briefly be
 * the previous event's while the new one loads — hence the id check).
 */
export function QcEventDetail(props: QcEventDetailProps) {
  const {
    event, detail, loading, error, cpsLimits, events, draft, onDraftChange, dirty, saving, saveError, onFlush,
    onToggleHidden, onResolveIssue, resolvingIssueId, onRestoreAi, actionBusy, getVideoTimeMs, newDraft,
  } = props;
  if (newDraft) return <NewEventEditor {...props} newDraft={newDraft} />;
  if (!event) {
    return <Text size="sm" c="dimmed">Select an event in the list to edit it. Double-click to jump the video to it.</Text>;
  }
  const d = detail && detail.id === event.id ? detail : undefined;
  const current = draft && draft.id === event.id ? draft : null;
  const timing: Timing = current
    ? { startMs: current.startMs, endMs: current.endMs }
    : { startMs: event.start_ms, endMs: event.end_ms };
  const matches = (type: 'original' | 'translated') =>
    (d?.watched_matches ?? []).filter((m) => m.word_type === type).map((m) => m.word);
  const shownText = current ? current.text : event.translated_text ?? '';
  const restorable = canRestoreAi(d?.original_ai_translated_text, shownText);
  const gaps = neighbourGaps(events, event, timing);

  return (
    <Stack gap={6} data-testid="qc-detail" data-event-id={event.id}>
      <Group gap={6} wrap="wrap">
        <Text size="xs" c="dimmed">#{event.line_index}</Text>
        {event.is_hidden && <Badge size="xs" variant="outline" color="gray">hidden</Badge>}
        {event.is_manual && <Badge size="xs" variant="outline" color="teal">manual</Badge>}
        {event.is_locked && <Badge size="xs" variant="outline" color="orange">locked</Badge>}
        {event.is_user_edited && <Badge size="xs" variant="outline" color="grape">user-edited</Badge>}
        {loading && !d && <Loader size={12} />}
        {d && <SeverityIndicators issues={d.issues} />}
        <div style={{ flex: 1 }} />
        <Text size="xs" c={saveError ? 'red' : 'dimmed'} data-testid="qc-save-status">
          {saving ? 'Saving…' : saveError ? 'Not saved' : dirty ? 'Unsaved changes' : 'Saved'}
        </Text>
      </Group>

      <Box onBlur={(e) => {
        // Focus left the whole editor (not just moved between its fields/buttons).
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onFlush();
      }}>
        <div className="qc-detail-wrap">
          <div className="qc-detail-grid">
            <Stack gap={6} className="qc-detail-main">
              <SourceBlock text={event.source_text} matches={matches('original')} />

              <Box>
                <Textarea
                  className="qc-detail-text qc-translation-text"
                  label="Translation"
                  size="xs"
                  autosize
                  minRows={2}
                  maxRows={8}
                  data-testid="qc-text"
                  data-autofocus
                  value={shownText}
                  rightSection={(
                    <Tooltip label="Restore AI translation" openDelay={300}>
                      <span>
                        <ActionIcon
                          size="sm"
                          variant="subtle"
                          color="gray"
                          data-testid="qc-restore-ai"
                          aria-label="Restore AI translation"
                          disabled={!restorable || actionBusy}
                          onClick={onRestoreAi}
                        >
                          <ArrowCounterClockwise size={14} />
                        </ActionIcon>
                      </span>
                    </Tooltip>
                  )}
                  rightSectionWidth={30}
                  rightSectionPointerEvents="all"
                  onChange={(e) => {
                    const text = e.currentTarget.value;
                    onDraftChange({ id: event.id, startMs: timing.startMs, endMs: timing.endMs, text });
                  }}
                  onBlur={onFlush}
                  onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onFlush(); } }}
                />
                {matches('translated').length > 0 && (
                  <Group gap={4} mt={4}>{matches('translated').map((w) => <WatchedWordBadge key={w} word={w} />)}</Group>
                )}
                {saveError && <Alert color="red" p="xs" mt={4} data-testid="qc-save-error">{saveError}</Alert>}
              </Box>

              {d?.original_ai_translated_text != null && d.original_ai_translated_text !== event.translated_text && (
                <Box>
                  <Text size="xs" c="dimmed" lh={1.2}>Original AI translation</Text>
                  <Text size="xs" lh={1.3} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{d.original_ai_translated_text}</Text>
                </Box>
              )}

              <TimingEditor
                key={event.id}
                prefix="qc"
                timing={timing}
                onChange={(t) => onDraftChange({
                  id: event.id, text: shownText,
                  startMs: t.startMs, endMs: t.endMs,
                })}
                onBlurred={onFlush}
                getVideoTimeMs={getVideoTimeMs}
                previousGapMs={gaps.previousMs}
                nextGapMs={gaps.nextMs}
                cps={<CpsReadout cps={event.cps} limits={cpsLimits} />}
                extra={(
                  <Button
                    size="compact-xs"
                    variant="subtle"
                    color="gray"
                    loading={actionBusy}
                    data-testid="qc-hide-toggle"
                    onClick={onToggleHidden}
                  >
                    {event.is_hidden ? 'Restore event' : 'Hide event'}
                  </Button>
                )}
              />
            </Stack>

            <div className="qc-meta" data-testid="qc-side">
              <QcEventMetadata event={event} d={d} loading={loading} />
              <hr className="qc-meta-sep" />
              <div data-testid="qc-qa">
                <QcIssuesPanel
                  d={d} event={event} loading={loading}
                  onResolveIssue={onResolveIssue} resolvingIssueId={resolvingIssueId}
                />
              </div>
            </div>
          </div>
        </div>
      </Box>

      {error && <Alert color="red" p="xs">Could not load the event detail.</Alert>}
    </Stack>
  );
}
