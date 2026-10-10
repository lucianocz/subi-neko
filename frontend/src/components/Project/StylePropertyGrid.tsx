import { useState } from 'react';
import type { ReactNode } from 'react';
import {
  ActionIcon,
  Autocomplete,
  Button,
  ColorPicker,
  Group,
  NumberInput,
  Popover,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { ArrowCounterClockwise, TextB, TextItalic, X } from '@phosphor-icons/react';
import { REPLACEMENT_FONT_GROUPS } from '../../constants/replacementFonts';
import type { ProjectStyle } from '../../hooks/useProjectStyles';
import {
  DEFAULT_ASS_COLOUR,
  assToCssRgba,
  assToHexa,
  describeAssColour,
  mergePickerValue,
  parseColourInput,
} from '../../utils/assColour.ts';
import {
  SIZE_RANGE,
  STYLE_GRID_COLUMNS,
  STYLE_GRID_MIN_WIDTH,
  STYLE_GRID_TEMPLATE,
  WIDTH_RANGE,
  cssFamily,
  cycleTriState,
  formatDecimal,
  parseDecimalInput,
  sourceRowCells,
  triStateLabel,
} from '../../utils/styleOverrides.ts';
import type { GridColumnId, SourceCell, StyleDraft, TriState } from '../../utils/styleOverrides.ts';
import './styleGrid.css';

interface StylePropertyGridProps {
  style: ProjectStyle;
  draft: StyleDraft;
  onPatch: (patch: Partial<StyleDraft>) => void;
}

function ResetIcon({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Tooltip label={label} openDelay={300}>
      <ActionIcon
        size={16}
        variant="subtle"
        color="blue"
        aria-label={label}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onClick}
      >
        <X size={11} />
      </ActionIcon>
    </Tooltip>
  );
}

function Swatch({
  value, readOnly, overridden, label, onClick,
}: {
  value: string | null;
  readOnly?: boolean;
  overridden?: boolean;
  label: string;
  onClick?: () => void;
}) {
  const title = describeAssColour(value ?? DEFAULT_ASS_COLOUR);
  return (
    <Tooltip label={`${label}: ${title}${overridden ? ' (override)' : readOnly ? '' : ' (inherited)'}`} openDelay={250}>
      <button
        type="button"
        className="sg-swatch"
        data-readonly={readOnly ? 'true' : undefined}
        data-overridden={overridden ? 'true' : undefined}
        aria-label={`${label}: ${title}`}
        disabled={readOnly}
        onClick={onClick}
      >
        <span className="sg-swatch-fill" style={{ background: assToCssRgba(value) }} />
      </button>
    </Tooltip>
  );
}

function SourceCellView({ cell, label }: { cell: SourceCell; label: string }) {
  if (cell.kind === 'text') {
    return <span className="sg-src-text" title={cell.text}>{cell.text}</span>;
  }
  if (cell.kind === 'flag') {
    return (
      <Tooltip label={cell.label} openDelay={250}>
        <span className="sg-src-flag" data-on={cell.on} aria-label={cell.label} role="img">
          {label === 'bold' ? <TextB size={16} weight="bold" /> : <TextItalic size={16} />}
        </span>
      </Tooltip>
    );
  }
  return <Swatch value={cell.value} readOnly label={label} />;
}

/** Colour swatch + popover: picker, exact hex entry (incl. alpha) and reset-to-source. */
function ColourCell({
  label, override, source, onChange,
}: {
  label: string;
  override: string | null;
  source: string | null;
  onChange: (value: string | null) => void;
}) {
  const [opened, setOpened] = useState(false);
  const [hexDraft, setHexDraft] = useState<string | null>(null);
  const effective = override ?? source ?? DEFAULT_ASS_COLOUR;
  const hexa = assToHexa(effective);

  function handlePicker(next: string) {
    const merged = mergePickerValue(effective, next);
    if (merged === null || (override === null && merged === effective)) return; // drift-only event
    if (merged !== override) onChange(merged);
  }

  function commitHex() {
    if (hexDraft === null) return;
    const parsed = parseColourInput(hexDraft);
    if (parsed && parsed !== effective) onChange(parsed);
    setHexDraft(null);
  }

  const hexInvalid = hexDraft !== null && parseColourInput(hexDraft) === null;

  return (
    <Popover opened={opened} onChange={setOpened} position="bottom" withArrow shadow="md" trapFocus>
      <Popover.Target>
        <div>
          <Swatch
            value={effective}
            overridden={override !== null}
            label={label}
            onClick={() => setOpened((o) => !o)}
          />
        </div>
      </Popover.Target>
      <Popover.Dropdown p="xs" w={236}>
        <Text size="xs" fw={600} mb={6}>{label}</Text>
        <ColorPicker format="hexa" value={hexa} onChange={handlePicker} fullWidth size="sm" />
        <TextInput
          mt="xs"
          size="xs"
          label="Hex (#RRGGBB or #RRGGBBAA)"
          aria-label={`${label} hex value`}
          value={hexDraft ?? hexa}
          error={hexInvalid ? 'Use #RGB, #RRGGBB or #RRGGBBAA' : undefined}
          onChange={(e) => setHexDraft(e.currentTarget.value)}
          onBlur={commitHex}
          onKeyDown={(e) => { if (e.key === 'Enter') commitHex(); }}
        />
        <Group justify="space-between" mt="xs" gap="xs">
          <Group gap={6}>
            <Swatch value={source} readOnly label="Source colour" />
            <Text size="xs" c="dimmed">Source</Text>
          </Group>
          <Button
            size="compact-xs"
            variant="light"
            leftSection={<ArrowCounterClockwise size={12} />}
            disabled={override === null}
            onClick={() => { onChange(null); setHexDraft(null); }}
          >
            Reset to source
          </Button>
        </Group>
      </Popover.Dropdown>
    </Popover>
  );
}

function TriStateButton({
  name, value, source, icon, onChange,
}: {
  name: string;
  value: TriState;
  source: boolean;
  icon: ReactNode;
  onChange: (v: TriState) => void;
}) {
  const state = value === null ? 'inherit' : value ? 'on' : 'off';
  const label = triStateLabel(name, value, source);
  return (
    <Tooltip label={label} openDelay={250}>
      <button
        type="button"
        className="sg-btn"
        data-state={state}
        aria-label={label}
        aria-pressed={value === null ? 'mixed' : value}
        onClick={() => onChange(cycleTriState(value))}
      >
        {icon}
      </button>
    </Tooltip>
  );
}

function DecimalCell({
  name, value, source, max, onChange,
}: {
  name: string;
  value: number | string;
  source: number;
  max: number;
  onChange: (v: number | string) => void;
}) {
  const overridden = parseDecimalInput(value) !== null;
  return (
    <NumberInput
      aria-label={`Replacement ${name}`}
      size="xs"
      placeholder={formatDecimal(source)}
      value={value}
      onChange={onChange}
      min={WIDTH_RANGE.min}
      max={max}
      step={0.25}
      decimalScale={4}
      allowNegative={false}
      hideControls
      classNames={{ input: 'sg-input' }}
      styles={{ input: { paddingInlineEnd: overridden ? 20 : 6, borderColor: overridden ? 'var(--mantine-color-blue-6)' : undefined } }}
      rightSectionWidth={20}
      rightSection={overridden ? <ResetIcon label={`Reset ${name} to source`} onClick={() => onChange('')} /> : null}
    />
  );
}

export function StylePropertyGrid({ style, draft, onPatch }: StylePropertyGridProps) {
  const src = sourceRowCells(style);
  const fontOverridden = draft.fontName.trim() !== '';
  const sizeOverridden = parseDecimalInput(draft.fontSize) !== null;

  const replacement: Record<Exclude<GridColumnId, 'label'>, ReactNode> = {
    font: (
      <Autocomplete
        aria-label="Replacement font"
        size="xs"
        w="100%"
        placeholder={style.font_name}
        data={REPLACEMENT_FONT_GROUPS}
        value={draft.fontName}
        onChange={(v) => onPatch({ fontName: v })}
        limit={60}
        classNames={{ input: 'sg-input' }}
        styles={{ input: { paddingInlineEnd: fontOverridden ? 22 : 6, borderColor: fontOverridden ? 'var(--mantine-color-blue-6)' : undefined } }}
        rightSectionWidth={22}
        rightSection={fontOverridden ? <ResetIcon label="Reset font to source" onClick={() => onPatch({ fontName: '' })} /> : null}
        renderOption={({ option }) => <span style={{ fontFamily: cssFamily(option.value) }}>{option.value}</span>}
      />
    ),
    size: (
      <NumberInput
        aria-label="Replacement size"
        size="xs"
        placeholder={formatDecimal(style.font_size)}
        value={draft.fontSize}
        onChange={(v) => onPatch({ fontSize: v })}
        min={SIZE_RANGE.min}
        max={SIZE_RANGE.max}
        decimalScale={1}
        allowNegative={false}
        hideControls
        classNames={{ input: 'sg-input' }}
        styles={{ input: { paddingInlineEnd: sizeOverridden ? 20 : 6, borderColor: sizeOverridden ? 'var(--mantine-color-blue-6)' : undefined } }}
        rightSectionWidth={20}
        rightSection={sizeOverridden ? <ResetIcon label="Reset size to source" onClick={() => onPatch({ fontSize: '' })} /> : null}
      />
    ),
    bold: (
      <TriStateButton
        name="Bold" value={draft.bold} source={style.bold} icon={<TextB size={16} weight="bold" />}
        onChange={(v) => onPatch({ bold: v })}
      />
    ),
    italic: (
      <TriStateButton
        name="Italic" value={draft.italic} source={style.italic} icon={<TextItalic size={16} />}
        onChange={(v) => onPatch({ italic: v })}
      />
    ),
    outline: (
      <DecimalCell
        name="outline" value={draft.outline} source={style.outline} max={WIDTH_RANGE.max}
        onChange={(v) => onPatch({ outline: v })}
      />
    ),
    shadow: (
      <DecimalCell
        name="shadow" value={draft.shadow} source={style.shadow} max={WIDTH_RANGE.max}
        onChange={(v) => onPatch({ shadow: v })}
      />
    ),
    primary: (
      <ColourCell
        label="Text colour" override={draft.primary} source={style.primary_colour}
        onChange={(v) => onPatch({ primary: v })}
      />
    ),
    outlineColour: (
      <ColourCell
        label="Border (outline) colour" override={draft.outlineColour} source={style.outline_colour}
        onChange={(v) => onPatch({ outlineColour: v })}
      />
    ),
    backColour: (
      <ColourCell
        label="Shadow colour" override={draft.backColour} source={style.back_colour}
        onChange={(v) => onPatch({ backColour: v })}
      />
    ),
  };

  const dataColumns = STYLE_GRID_COLUMNS.filter((c) => c.id !== 'label');
  const colourLabels: Partial<Record<GridColumnId, string>> = {
    primary: 'Source text colour', outlineColour: 'Source border colour', backColour: 'Source shadow colour',
  };

  return (
    <div className="sg-scroll">
      <div
        className="sg-grid"
        role="group"
        aria-label="Style properties"
        style={{ gridTemplateColumns: STYLE_GRID_TEMPLATE, minWidth: STYLE_GRID_MIN_WIDTH }}
      >
        {STYLE_GRID_COLUMNS.map((c) => (
          <Tooltip key={`h-${c.id}`} label={c.hint ?? c.header} disabled={!c.hint} openDelay={300}>
            <div className="sg-head" data-id={c.id}>{c.header}</div>
          </Tooltip>
        ))}

        <div className="sg-row-label">Source</div>
        {dataColumns.map((c) => (
          <div key={`s-${c.id}`} className="sg-cell" data-id={c.id}>
            <SourceCellView
              cell={src[c.id as Exclude<GridColumnId, 'label'>]}
              label={colourLabels[c.id] ?? c.id}
            />
          </div>
        ))}

        <div className="sg-row-label">Replacement</div>
        {dataColumns.map((c) => (
          <div key={`r-${c.id}`} className="sg-cell" data-id={c.id}>
            {replacement[c.id as Exclude<GridColumnId, 'label'>]}
          </div>
        ))}
      </div>
    </div>
  );
}
