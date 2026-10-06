import { useState } from 'react';
import { Alert, Badge, Box, Button, Group, Loader, NativeSelect, Stack, Text, Textarea, TextInput } from '@mantine/core';
import type { QcEvent, QcEventDetail as QcEventDetailDto } from '../../types/qc';
import type { EventDraft, NewEventDraft } from '../../utils/qcDraft';
import { validateNewEvent } from '../../utils/qcDraft';
import { ArrowCounterClockwise, CheckCircle } from '@phosphor-icons/react';
import { CPS_COLORS, cpsSeverity } from '../../utils/cps';
import type { CpsLimits } from '../../utils/cps';
import { canRestoreAi } from '../../utils/qcQa';
import { SEVERITY_COLORS } from '../../utils/qaSeverity';
import { formatMs } from '../../utils/qcText';
import { formatTimeField, nudgeBoundary, parseTimeField, setBoundary } from '../../utils/qcTime';
import type { Timing } from '../../utils/qcTime';
import { WatchedWordBadge } from '../WatchedWordBadge';
import { GENDER_COLORS, NON_BINARY_BADGE_STYLE } from '../../utils/gender';

const NUDGE_MS = 100;

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Box>
      <Text size="xs" c="dimmed">{label}</Text>
      <Text size="sm" component="div">{children}</Text>
    </Box>
  );
}

/** Raw source text in a read-only (selectable, copyable) textarea — never `disabled`. */
function SourceBlock({ text, matches }: { text: string | null; matches: string[] }) {
  return (
    <Box>
      <Textarea
        className="qc-detail-text qc-source-text"
        label="Source"
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
  if (!gender) return <Text span size="sm" c="dimmed">not set</Text>;
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

/** Read-only facts about the selected event (right column of the detail panel). */
function QcEventMetadata({
  event, d, cpsLimits, loading,
}: {
  event: QcEvent;
  d: QcEventDetailDto | undefined;
  cpsLimits: CpsLimits;
  loading: boolean;
}) {
  const cpsLevel = cpsSeverity(event.cps, cpsLimits);
  const timingChanged = d && (d.original_start_ms !== event.start_ms || d.original_end_ms !== event.end_ms);
  // Same identity rule as the subtitle editor: mapped character's gender, else the speaker's.
  const gender = d ? (d.character_name ? d.character_gender : d.speaker_gender) : null;
  return (
    <div className="qc-meta" data-testid="qc-meta">
      <MetaRow label="Character">
        <Text span fw={600} data-testid="qc-character">{d ? d.character_name ?? '—' : loading ? '…' : '—'}</Text>
      </MetaRow>
      <MetaRow label="Gender">{d ? <GenderBadge gender={gender} /> : '…'}</MetaRow>
      <hr className="qc-meta-sep" />
      <MetaRow label="Speaker">{event.speaker ?? '—'}</MetaRow>
      <MetaRow label="Style">{event.style}</MetaRow>
      <MetaRow label="Content">{event.content_type}</MetaRow>
      <MetaRow label="Event type">{event.event_type}</MetaRow>
      {d && <MetaRow label="Layer">{d.layer}</MetaRow>}
      <hr className="qc-meta-sep" />
      <MetaRow label="CPS">
        {event.cps == null ? '—' : (
          <Text
            span size="sm" fw={cpsLevel === 'normal' ? 400 : 700} c={CPS_COLORS[cpsLevel]}
            data-testid="qc-cps" data-cps-over={cpsLevel === 'error' ? 'true' : 'false'}
            data-cps-level={cpsLevel}
          >
            {event.cps.toFixed(1)}
            {cpsLevel === 'error' ? ` (limit ${cpsLimits.hard})` : cpsLevel === 'warning' ? ` (soft ${cpsLimits.soft})` : ''}
          </Text>
        )}
      </MetaRow>
      {d && (
        <MetaRow label="Original timing">
          <code data-testid="qc-original-timing">{formatMs(d.original_start_ms)} → {formatMs(d.original_end_ms)}</code>
          {timingChanged && <Text span size="xs" c="orange" ml={4}>(changed)</Text>}
        </MetaRow>
      )}
    </div>
  );
}

/** `HH:MM:SS.mmm` input: free typing locally, parsed on blur / Enter. */
function TimeField({
  label, valueMs, onCommit, onBlurred, testId,
}: {
  label: string;
  valueMs: number;
  onCommit: (ms: number) => void;
  onBlurred: () => void;
  testId: string;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const error = typed !== null && parseTimeField(typed) === null;

  const commit = () => {
    if (typed === null) return;
    const ms = parseTimeField(typed);
    if (ms !== null) {
      setTyped(null);
      if (ms !== valueMs) onCommit(ms);
    }
  };

  return (
    <TextInput
      className="qc-time-input"
      size="xs"
      label={label}
      data-testid={testId}
      value={typed ?? formatTimeField(valueMs)}
      error={error ? 'h:mm:ss.mmm' : undefined}
      onFocus={(e) => { setTyped(formatTimeField(valueMs)); e.currentTarget.select(); }}
      onChange={(e) => setTyped(e.currentTarget.value)}
      onKeyDown={(e) => { if (e.key === 'Enter') { commit(); e.currentTarget.blur(); } }}
      onBlur={() => { commit(); setTyped(null); onBlurred(); }}
    />
  );
}

function TimingEditor({
  timing, onChange, onBlurred, getVideoTimeMs, prefix,
}: {
  timing: Timing;
  onChange: (t: Timing) => void;
  onBlurred: () => void;
  getVideoTimeMs: () => number | null;
  prefix: string;
}) {
  const [hint, setHint] = useState<string | null>(null);
  const apply = (result: ReturnType<typeof setBoundary>) => {
    if (result.ok) {
      setHint(null);
      onChange(result.timing);
    } else setHint(result.reason);
  };
  const toCurrent = (which: 'start' | 'end') => {
    const ms = getVideoTimeMs();
    if (ms === null) return setHint('Video is not ready.');
    apply(setBoundary(timing, which, ms));
  };

  const column = (which: 'start' | 'end', label: string, valueMs: number) => (
    <Stack gap={4}>
      <TimeField
        label={label}
        valueMs={valueMs}
        testId={`${prefix}-${which}`}
        onCommit={(ms) => apply(setBoundary(timing, which, ms))}
        onBlurred={onBlurred}
      />
      <Group gap={4} wrap="nowrap">
        <Button size="compact-xs" variant="default" title={`${label} −${NUDGE_MS} ms`} data-testid={`${prefix}-${which}-minus`}
          onClick={() => apply(nudgeBoundary(timing, which, -NUDGE_MS))}>−{NUDGE_MS}</Button>
        <Button size="compact-xs" variant="default" title={`${label} +${NUDGE_MS} ms`} data-testid={`${prefix}-${which}-plus`}
          onClick={() => apply(nudgeBoundary(timing, which, NUDGE_MS))}>+{NUDGE_MS}</Button>
        <Button size="compact-xs" variant="light" data-testid={`${prefix}-${which}-current`}
          onClick={() => toCurrent(which)}>Set {which} to current</Button>
      </Group>
    </Stack>
  );

  return (
    <Stack gap={4}>
      <Group gap="md" align="flex-start">
        {column('start', 'Start', timing.startMs)}
        {column('end', 'End', timing.endMs)}
        <Field label="Duration">{((timing.endMs - timing.startMs) / 1000).toFixed(2)} s</Field>
      </Group>
      {hint && <Text size="xs" c="red">{hint}</Text>}
    </Stack>
  );
}

export interface QcEventDetailProps {
  event: QcEvent | null;
  detail: QcEventDetailDto | undefined;
  loading: boolean;
  error: boolean;
  cpsLimits: CpsLimits;
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
    event, detail, loading, error, cpsLimits, draft, onDraftChange, dirty, saving, saveError, onFlush,
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
  const unresolved = d ? d.issues.filter((i) => !i.is_resolved).length : null;
  const shownText = current ? current.text : event.translated_text ?? '';
  const restorable = canRestoreAi(d?.original_ai_translated_text, shownText);

  return (
    <Stack gap="xs" data-testid="qc-detail" data-event-id={event.id}>
      <Group gap={6} wrap="wrap">
        <Text size="xs" c="dimmed">#{event.line_index}</Text>
        {event.is_hidden && <Badge size="xs" variant="outline" color="gray">hidden</Badge>}
        {event.is_manual && <Badge size="xs" variant="outline" color="teal">manual</Badge>}
        {event.is_locked && <Badge size="xs" variant="outline" color="orange">locked</Badge>}
        {event.is_user_edited && <Badge size="xs" variant="outline" color="grape">user-edited</Badge>}
        {loading && !d && <Loader size={12} />}
        <div style={{ flex: 1 }} />
        <Text size="xs" c={saveError ? 'red' : 'dimmed'} data-testid="qc-save-status">
          {saving ? 'Saving…' : saveError ? 'Not saved' : dirty ? 'Unsaved changes' : 'Saved'}
        </Text>
        <Button
          size="compact-xs"
          variant="default"
          color={event.is_hidden ? undefined : 'red'}
          loading={actionBusy}
          data-testid="qc-hide-toggle"
          onClick={onToggleHidden}
        >
          {event.is_hidden ? 'Restore' : 'Hide'}
        </Button>
      </Group>

      <div className="qc-detail-wrap">
        <div className="qc-detail-grid">
          <Stack gap="xs" className="qc-detail-main">
          <SourceBlock text={event.source_text} matches={matches('original')} />

          <Box onBlur={(e) => {
            // Focus left the whole editor (not just moved between its fields/buttons).
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onFlush();
          }}>
            <Box pos="relative">
            {restorable && (
              <Button
                size="compact-xs"
                variant="subtle"
                color="gray"
                data-testid="qc-restore-ai"
                title="Revert to the original AI translation"
                leftSection={<ArrowCounterClockwise size={12} />}
                disabled={actionBusy}
                style={{ position: 'absolute', top: -2, right: 0, zIndex: 1 }}
                onClick={onRestoreAi}
              >
                Restore AI translation
              </Button>
            )}
            <Textarea
              className="qc-detail-text"
              label="Translation"
              autosize
              minRows={2}
              maxRows={8}
              data-testid="qc-text"
              data-autofocus
              value={current ? current.text : event.translated_text ?? ''}
              onChange={(e) => {
                const text = e.currentTarget.value;
                onDraftChange({ id: event.id, startMs: timing.startMs, endMs: timing.endMs, text });
              }}
              onBlur={onFlush}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onFlush(); } }}
            />
            </Box>
            {matches('translated').length > 0 && (
              <Group gap={4} mt={4}>{matches('translated').map((w) => <WatchedWordBadge key={w} word={w} />)}</Group>
            )}
            {saveError && <Alert color="red" p="xs" mt={4} data-testid="qc-save-error">{saveError}</Alert>}

            <Box mt="xs">
              <TimingEditor
                prefix="qc"
                timing={timing}
                onChange={(t) => onDraftChange({
                  id: event.id, text: current ? current.text : event.translated_text ?? '',
                  startMs: t.startMs, endMs: t.endMs,
                })}
                onBlurred={onFlush}
                getVideoTimeMs={getVideoTimeMs}
              />
            </Box>
          </Box>
          </Stack>
          <QcEventMetadata event={event} d={d} cpsLimits={cpsLimits} loading={loading} />
        </div>
      </div>

      {d?.original_ai_translated_text != null && d.original_ai_translated_text !== event.translated_text && (
        <Box>
          <Text size="xs" c="dimmed">Original AI translation</Text>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{d.original_ai_translated_text}</Text>
        </Box>
      )}

      {error && <Alert color="red" p="xs">Could not load the event detail.</Alert>}

      <Box data-testid="qc-qa">
        {!d || d.issues.length === 0 ? (
          <Text size="xs" c="dimmed" data-testid="qc-qa-heading">
            QA issues{!d && event.issue_count > 0 ? ` (${event.issue_count} unresolved)` : !d && loading ? ' …' : d ? ': none' : ''}
          </Text>
        ) : (
          <>
            <Text size="xs" c="dimmed" mb={2} data-testid="qc-qa-heading">
              QA issues ({unresolved} unresolved)
            </Text>
            <Stack gap={4}>
              {d.issues.map((issue) => (
                <Group
                  key={issue.id} gap={6} wrap="nowrap" align="flex-start"
                  data-testid="qc-issue" data-resolved={issue.is_resolved ? 'true' : 'false'}
                  style={{ opacity: issue.is_resolved ? 0.55 : 1 }}
                >
                  <Badge size="xs" variant="light" color={SEVERITY_COLORS[issue.severity.toLowerCase()] ?? 'gray'}>{issue.severity}</Badge>
                  <Text size="xs" c="dimmed" style={{ flex: '0 0 auto' }}>{issue.qa_type}</Text>
                  <Text size="sm" style={{ minWidth: 0, flex: 1, wordBreak: 'break-word' }}>
                    {issue.message}
                  </Text>
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
              ))}
            </Stack>
          </>
        )}
      </Box>
    </Stack>
  );
}
