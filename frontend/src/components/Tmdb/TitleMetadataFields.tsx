import { Autocomplete, Button, Grid, Group, NumberInput, Stack, Textarea, TextInput } from '@mantine/core';
import type { WebConfig } from '../../hooks/useWebPublishing';

type Metadata = WebConfig['metadata'];

export interface TitleMetadataFieldsProps {
  config: WebConfig;
  /** TMDB romanizations; more than one turns the Romaji field into a picker. */
  names: string[];
  onField: <K extends keyof Metadata>(key: K, value: Metadata[K]) => void;
  onTranslate: () => void;
  translating: boolean;
  /** Disables "Translate with AI" while another request owns the form. */
  busy: boolean;
  /** Import wizard: short fields keep a fixed width instead of a grid share. */
  compact?: boolean;
  /** Inline validation messages (compact mode). */
  errors?: { title?: string; language?: string };
}

/** The approved-title editor shared by the WEB Publish wizard (default layout,
 * unchanged) and the project import wizard (`compact`). */
export function TitleMetadataFields(
  { config, names, onField, onTranslate, translating, busy, compact = false, errors }: TitleMetadataFieldsProps,
) {
  const m = config.metadata;
  const title = <TextInput label="English title" required maxLength={255} value={m.title_english}
    error={errors?.title} onChange={e => onField('title_english', e.currentTarget.value)} />;
  const year = <NumberInput label="Year" min={1} max={9999} allowDecimal={false} value={m.year ?? ''}
    onChange={v => onField('year', typeof v === 'number' ? v : null)} />;
  const romaji = names.length > 1
    ? <Autocomplete label="Romaji title" placeholder="Choose a name or enter your own" data={names} maxLength={255}
      value={m.title_romaji ?? ''} onChange={v => onField('title_romaji', v || null)} />
    : <TextInput label="Romaji title" maxLength={255} value={m.title_romaji ?? ''}
      onChange={e => onField('title_romaji', e.currentTarget.value || null)} />;
  const language = <TextInput label="Language" required maxLength={3} value={m.original_language}
    error={errors?.language} onChange={e => onField('original_language', e.currentTarget.value)} />;
  const translate = <Group justify="flex-end"><Button size="compact-xs" variant="subtle" loading={translating}
    disabled={busy || !m.original_description?.trim()} onClick={onTranslate}>Translate with AI</Button></Group>;

  if (compact) {
    return <Stack style={{ flex: 1, minWidth: 0 }} gap="xs">
      <Group gap="xs" align="flex-start" wrap="nowrap">
        <div style={{ flex: 1, minWidth: 0 }}>{title}</div>
        <div style={{ width: 92, flexShrink: 0 }}>{year}</div>
      </Group>
      <Group gap="xs" align="flex-start" wrap="nowrap">
        <div style={{ flex: 1, minWidth: 0 }}>{romaji}</div>
        <div style={{ width: 92, flexShrink: 0 }}>{language}</div>
      </Group>
      <Textarea label="Description" autosize minRows={3} maxRows={6} maxLength={10000} value={m.description ?? ''}
        onChange={e => onField('description', e.currentTarget.value || null)} />
      {translate}
    </Stack>;
  }

  return <Stack style={{ flex: 1, minWidth: 240 }} gap="xs">
    <Grid columns={10} gap="xs">
      <Grid.Col span={8}>{title}</Grid.Col>
      <Grid.Col span={2}>{year}</Grid.Col>
      <Grid.Col span={8}>{romaji}</Grid.Col>
      <Grid.Col span={2}>{language}</Grid.Col>
    </Grid>
    <Textarea label="Description" rows={5} maxLength={10000} value={m.description ?? ''}
      onChange={e => onField('description', e.currentTarget.value || null)} />
    {translate}
  </Stack>;
}
