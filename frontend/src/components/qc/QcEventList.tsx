import { memo, useEffect, useRef } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Badge } from '@mantine/core';
import type { QcEvent } from '../../types/qc';
import type { ActiveEvents } from '../../hooks/useActiveSubtitleEvents';
import { formatMs, plainAssText } from '../../utils/qcText';
import { CPS_COLORS, cpsSeverity } from '../../utils/cps';
import type { CpsLimits } from '../../utils/cps';
import { SEVERITY_COLORS } from '../../utils/qaSeverity';
import { WATCHED_ROW_BACKGROUND, WATCHED_WORD_BADGE } from '../../utils/watchedWords';
import './qc.css';

// Fixed height: meta line + translated line + source line. Fixed rows keep the
// virtualizer trivially cheap and let the Follow check be pure arithmetic.
export const QC_ROW_HEIGHT = 60;


const SCROLL_KEYS = new Set(['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown']);

interface RowProps {
  event: QcEvent;
  top: number;
  selected: boolean;
  active: boolean;
  cpsLimits: CpsLimits;
  onSelect: (event: QcEvent) => void;
  onSeek: (event: QcEvent) => void;
}

const Row = memo(function Row({ event, top, selected, active, cpsLimits, onSelect, onSeek }: RowProps) {
  const translated = plainAssText(event.translated_text);
  const cpsLevel = cpsSeverity(event.cps, cpsLimits);
  return (
    <div
      className="qc-row"
      data-selected={selected}
      data-active={active}
      data-hidden={event.is_hidden}
      data-event-id={event.id}
      data-watched={event.watched_count > 0}
      style={{ top, height: QC_ROW_HEIGHT, '--qc-watched-bg': WATCHED_ROW_BACKGROUND } as CSSProperties}
      onClick={() => onSelect(event)}
      onDoubleClick={() => onSeek(event)}
    >
      <div className="qc-row-meta">
        <span className="qc-row-time">{formatMs(event.start_ms)}</span>
        <span>→ {formatMs(event.end_ms)}</span>
        <span title={`Style: ${event.style} · ${event.content_type}`}>
          {event.content_type !== 'dialogue' ? `${event.content_type} · ` : ''}{event.style}
        </span>
        {event.cps != null && (
          <Badge
            size="xs"
            variant={cpsLevel === 'error' ? 'filled' : 'light'}
            color={CPS_COLORS[cpsLevel] ?? 'gray'}
            title="Characters per second"
            data-testid="qc-row-cps"
            data-cps-level={cpsLevel}
          >
            {event.cps.toFixed(1)} cps
          </Badge>
        )}
        {event.issue_count > 0 && (
          <Badge size="xs" variant="light" color={SEVERITY_COLORS[(event.max_issue_severity ?? '').toLowerCase()] ?? 'gray'} title="Unresolved QA issues">
            ⚠ {event.issue_count}
          </Badge>
        )}
        {event.watched_count > 0 && (
          <Badge {...WATCHED_WORD_BADGE} title="Watched-word matches">
            👁 {event.watched_count}
          </Badge>
        )}
        {event.is_hidden && <Badge size="xs" variant="outline" color="gray">hidden</Badge>}
        {event.is_manual && <Badge size="xs" variant="outline" color="teal">manual</Badge>}
      </div>
      <div className={`qc-row-text${translated ? '' : ' qc-empty-text'}`}>
        {translated || '(no translation)'}
      </div>
      <div className="qc-row-source">{plainAssText(event.source_text)}</div>
    </div>
  );
});

/**
 * Virtualized list of ALL events. Rows are plain divs (no inputs/textareas), so
 * only the ~20 visible rows exist in the DOM however long the file is.
 *
 * Selected (outline) and active (fill) are separate props and separate styles.
 * Follow: when the active set changes and none of its rows is visible, the first
 * one (by start) is scrolled into view. A *user* scroll (wheel, touch, scrollbar
 * drag, scroll keys) calls `onUserScroll`; programmatic scrolls never do, because
 * they are not input events — so no suppression flag is needed.
 */
export function QcEventList({
  events, cpsLimits, active, selectedId, follow, reveal, onSelect, onSeek, onUserScroll,
}: {
  events: readonly QcEvent[];
  cpsLimits: CpsLimits;
  active: ActiveEvents;
  selectedId: number | null;
  follow: boolean;
  /** Scroll this event into view if it is off-screen (e.g. after its timing moved it). */
  reveal: { id: number; nonce: number } | null;
  onSelect: (event: QcEvent) => void;
  onSeek: (event: QcEvent) => void;
  onUserScroll: () => void;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => QC_ROW_HEIGHT,
    overscan: 12,
    getItemKey: (i) => events[i].id,
  });

  useEffect(() => {
    if (!follow || active.indices.length === 0) return;
    const el = parentRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const bottom = top + el.clientHeight;
    const anyVisible = active.indices.some(
      (i) => i * QC_ROW_HEIGHT < bottom && (i + 1) * QC_ROW_HEIGHT > top);
    if (!anyVisible) virtualizer.scrollToIndex(active.indices[0], { align: 'center' });
  }, [active, follow, virtualizer]);

  useEffect(() => {
    if (!reveal) return;
    const i = events.findIndex((e) => e.id === reveal.id);
    if (i >= 0) virtualizer.scrollToIndex(i, { align: 'auto' });
    // Only a new reveal request (nonce) scrolls; ordinary list updates must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.nonce]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (SCROLL_KEYS.has(e.key)) onUserScroll();
  };

  return (
    <div
      ref={parentRef}
      className="qc-list"
      tabIndex={0}
      aria-label="Subtitle events"
      onWheel={onUserScroll}
      onTouchMove={onUserScroll}
      onKeyDown={onKeyDown}
      // A press on the container itself (not a row) is the scrollbar.
      onPointerDown={(e) => { if (e.target === e.currentTarget) onUserScroll(); }}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
        {virtualizer.getVirtualItems().map((item) => {
          const event = events[item.index];
          return (
            <Row
              key={event.id}
              event={event}
              top={item.start}
              selected={event.id === selectedId}
              active={active.ids.has(event.id)}
              cpsLimits={cpsLimits}
              onSelect={onSelect}
              onSeek={onSeek}
            />
          );
        })}
      </div>
    </div>
  );
}
