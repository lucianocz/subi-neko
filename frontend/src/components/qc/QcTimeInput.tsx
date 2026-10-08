import { useState } from 'react';
import { ActionIcon, Group, Input, TextInput, Tooltip } from '@mantine/core';
import { Crosshair, Minus, Plus } from '@phosphor-icons/react';
import { DEFAULT_TIME_STEP_MS, parseTimeInput, roundToPrecision } from '../../utils/qcDetail';
import { formatTimeField } from '../../utils/qcTime';

const CONTROL_HEIGHT = 30; // Mantine `xs` input height — the icon buttons match it.

export interface QcTimeInputProps {
  label: string;
  valueMs: number;
  /** Apply a new time; returns an error message when the edit is refused. */
  onChange: (ms: number) => string | null;
  /** Called when focus leaves the input (blur-save hook). */
  onBlurred?: () => void;
  /** ± button step (the result is kept on the backend's 10 ms grid). */
  stepMs?: number;
  /** Current video time in ms (null = not ready); omit to hide the button. */
  getCurrentMs?: () => number | null;
  description?: React.ReactNode;
  descriptionColor?: string;
  testId: string;
}

/** `[ − ] [ HH:MM:SS.mmm ] [ + ] [ ⌖ ]` — free typing, parsed on Enter / blur. */
export function QcTimeInput({
  label, valueMs, onChange, onBlurred, stepMs = DEFAULT_TIME_STEP_MS, getCurrentMs, description, descriptionColor, testId,
}: QcTimeInputProps) {
  const [typed, setTyped] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const parsed = typed === null ? null : parseTimeInput(typed);
  const typedError = parsed && !parsed.ok ? parsed.reason : null;
  const error = typedError ?? hint;

  const apply = (ms: number) => {
    const refused = onChange(ms);
    setHint(refused);
    return refused === null;
  };
  const commit = () => {
    if (typed === null || !parsed || !parsed.ok) return;
    setTyped(null);
    if (parsed.ms !== valueMs) apply(parsed.ms);
    else setHint(null);
  };
  const setToCurrent = () => {
    const ms = getCurrentMs?.() ?? null;
    if (ms === null) return setHint('Video is not ready.');
    apply(roundToPrecision(ms));
  };

  return (
    <Input.Wrapper
      label={label}
      size="xs"
      description={error ? undefined : description}
      error={error}
      inputWrapperOrder={['label', 'input', 'description', 'error']}
      styles={{ description: { color: descriptionColor }, root: { flex: '1 1 210px', minWidth: 190 } }}
    >
      <Group gap={4} wrap="nowrap" data-testid={`${testId}-field`}>
        <Tooltip label={`${label} −${stepMs} ms`} openDelay={400}>
          <ActionIcon
            variant="default" size={CONTROL_HEIGHT} aria-label={`${label} minus ${stepMs} ms`} data-testid={`${testId}-minus`}
            onClick={() => apply(Math.max(0, roundToPrecision(valueMs - stepMs)))}
          >
            <Minus size={14} />
          </ActionIcon>
        </Tooltip>
        <TextInput
          className="qc-time-input"
          size="xs"
          aria-label={label}
          aria-invalid={typedError ? true : undefined}
          data-testid={testId}
          value={typed ?? formatTimeField(valueMs)}
          error={typedError ? true : undefined}
          onFocus={(e) => { setTyped(formatTimeField(valueMs)); e.currentTarget.select(); }}
          onChange={(e) => { setHint(null); setTyped(e.currentTarget.value); }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { commit(); e.currentTarget.blur(); }
            else if (e.key === 'Escape') { setTyped(null); e.currentTarget.blur(); }
          }}
          onBlur={() => {
            // An unparsable entry is discarded (never reinterpreted) but the reason stays visible.
            if (typedError) setHint(`${typedError} — not applied`);
            commit(); setTyped(null); onBlurred?.();
          }}
        />
        <Tooltip label={`${label} +${stepMs} ms`} openDelay={400}>
          <ActionIcon
            variant="default" size={CONTROL_HEIGHT} aria-label={`${label} plus ${stepMs} ms`} data-testid={`${testId}-plus`}
            onClick={() => apply(roundToPrecision(valueMs + stepMs))}
          >
            <Plus size={14} />
          </ActionIcon>
        </Tooltip>
        {getCurrentMs && (
          <Tooltip label={`Set ${label.toLowerCase()} to current video time`} openDelay={400}>
            <ActionIcon
              variant="light" size={CONTROL_HEIGHT} aria-label={`Set ${label.toLowerCase()} to current video time`}
              data-testid={`${testId}-current`}
              onClick={setToCurrent}
            >
              <Crosshair size={16} />
            </ActionIcon>
          </Tooltip>
        )}
      </Group>
    </Input.Wrapper>
  );
}
