import { useMemo, useState } from 'react';
import {
  Alert,
  Autocomplete,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  NumberInput,
  Paper,
  Stack,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { TextAa } from '@phosphor-icons/react';
import { FONT_PREVIEW_LINES, REPLACEMENT_FONT_GROUPS } from '../constants/replacementFonts';
import { useProjectStyles, useUpdateProjectStyle } from '../hooks/useProjectStyles';
import type { ProjectStyle } from '../hooks/useProjectStyles';

interface ProjectStylesDialogProps {
  projectId: number;
  opened: boolean;
  onClose: () => void;
}

function formatSize(size: number) {
  return Number.isInteger(size) ? String(size) : size.toFixed(1);
}

/** "Arial 42 → Noto Sans 40"; falls back to the source value per field. */
function fontSummary(style: ProjectStyle) {
  const source = `${style.font_name} ${formatSize(style.font_size)}`;
  if (!style.replacement_font_name && style.replacement_font_size == null) return source;
  const name = style.replacement_font_name || style.font_name;
  const size = style.replacement_font_size ?? style.font_size;
  return `${source} → ${name} ${formatSize(size)}`;
}

function cssFamily(name: string) {
  return `"${name.replace(/["\\]/g, '')}", sans-serif`;
}

function StyleEditor({ projectId, style }: { projectId: number; style: ProjectStyle }) {
  const update = useUpdateProjectStyle(projectId);
  const [fontName, setFontName] = useState(style.replacement_font_name ?? '');
  const [fontSize, setFontSize] = useState<number | ''>(style.replacement_font_size ?? '');

  // Same fallback semantics as translated ASS export.
  const effectiveName = fontName.trim() || style.font_name;
  const effectiveSize = fontSize === '' ? style.font_size : fontSize;

  const dirty =
    (fontName.trim() || null) !== style.replacement_font_name
    || (fontSize === '' ? null : fontSize) !== style.replacement_font_size;

  async function save() {
    try {
      await update.mutateAsync({
        styleId: style.id,
        replacement_font_name: fontName.trim() || null,
        replacement_font_size: fontSize === '' ? null : fontSize,
      });
      notifications.show({
        color: 'green',
        message: `Style "${style.style_name}" updated for ${style.file_count} file${style.file_count === 1 ? '' : 's'}.`,
      });
    } catch {
      notifications.show({ color: 'red', title: 'Save failed', message: 'Could not update the style.' });
    }
  }

  return (
    <Stack gap="md" h="100%" style={{ minHeight: 0 }}>
      <Group grow align="flex-start" style={{ flex: '0 0 auto' }}>
        <TextInput label="Style name" value={style.style_name} readOnly />
        <TextInput label="Source font" value={style.font_name} readOnly />
        <TextInput label="Source size" value={formatSize(style.font_size)} readOnly />
      </Group>

      <Text size="xs" c="dimmed" style={{ flex: '0 0 auto' }}>
        The replacement applies to translated subtitles in all {style.file_count} file
        {style.file_count === 1 ? '' : 's'} using this style. Source subtitles keep the original font.
        Leave a field empty to fall back to the source value.
      </Text>

      <Group grow align="flex-start" style={{ flex: '0 0 auto' }}>
        <Autocomplete
          label="Replacement font"
          placeholder={style.font_name}
          data={REPLACEMENT_FONT_GROUPS}
          value={fontName}
          onChange={setFontName}
          limit={60}
          renderOption={({ option }) => <span style={{ fontFamily: cssFamily(option.value) }}>{option.value}</span>}
        />
        <NumberInput
          label="Replacement size"
          placeholder={formatSize(style.font_size)}
          value={fontSize}
          onChange={(v) => setFontSize(typeof v === 'number' ? v : '')}
          min={1}
          max={1000}
          decimalScale={1}
          allowNegative={false}
        />
      </Group>

      {/* Bounded: the sample keeps its real size and scrolls here instead of growing the dialog. */}
      <Paper
        withBorder
        p="md"
        radius="sm"
        style={{ flex: '1 1 0', minHeight: 80, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
      >
        <Group justify="space-between" mb="xs" style={{ flex: '0 0 auto' }}>
          <Text size="xs" c="dimmed">Preview (translated)</Text>
          <Badge size="sm" variant="light" tt="none">{effectiveName} {formatSize(effectiveSize)}</Badge>
        </Group>
        <Box
          style={{
            flex: '1 1 0',
            minHeight: 0,
            overflow: 'auto',
            overflowWrap: 'anywhere',
            fontFamily: cssFamily(effectiveName),
            fontSize: Math.max(effectiveSize, 10),
            lineHeight: 1.3,
          }}
        >
          {FONT_PREVIEW_LINES.map((line) => <div key={line}>{line}</div>)}
        </Box>
      </Paper>

      <Group justify="flex-end" style={{ flex: '0 0 auto' }}>
        <Button
          variant="subtle"
          color="gray"
          disabled={!fontName && fontSize === ''}
          onClick={() => { setFontName(''); setFontSize(''); }}
        >
          Use source font
        </Button>
        <Button onClick={() => void save()} disabled={!dirty} loading={update.isPending}>
          Save
        </Button>
      </Group>
    </Stack>
  );
}

export function ProjectStylesDialog({ projectId, opened, onClose }: ProjectStylesDialogProps) {
  const { data: styles = [], isLoading, isError } = useProjectStyles(projectId, opened);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return styles;
    return styles.filter((s) => (
      `${s.style_name} ${s.font_name} ${s.replacement_font_name ?? ''}`.toLowerCase().includes(q)
    ));
  }, [styles, search]);

  const selected = styles.find((s) => s.id === selectedId) ?? filtered[0] ?? null;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      size="min(1120px, 94vw)"
      styles={{
        content: { height: 'min(82vh, 860px)', display: 'flex', flexDirection: 'column' },
        body: { flex: '1 1 0', minHeight: 0, display: 'flex', flexDirection: 'column' },
      }}
      title={(
        <Group gap="xs">
          <TextAa size={18} />
          <Text fw={600}>Styles &amp; fonts</Text>
        </Group>
      )}
    >
      {isLoading ? (
        <Center py="xl"><Loader size="sm" /></Center>
      ) : isError ? (
        <Alert color="red">Could not load styles.</Alert>
      ) : styles.length === 0 ? (
        <Text c="dimmed" size="sm">No styles yet — subtitles have not been extracted.</Text>
      ) : (
        <Group align="stretch" wrap="nowrap" gap="md" style={{ flex: '1 1 0', minHeight: 0 }}>
          <Stack gap="xs" w={340} miw={300} maw={340} style={{ flexShrink: 0, minHeight: 0 }}>
            <TextInput
              placeholder="Search styles"
              value={search}
              onChange={(e) => setSearch(e.currentTarget.value)}
            />
            {/* Plain overflow box: Mantine's ScrollArea content is display:table and grows with long names. */}
            <Box style={{ flex: '1 1 0', minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
              <Stack gap={4}>
                {filtered.map((style) => {
                  const active = style.id === selected?.id;
                  return (
                    <UnstyledButton
                      key={style.id}
                      onClick={() => setSelectedId(style.id)}
                      p="xs"
                      style={{
                        display: 'block',
                        width: '100%',
                        minWidth: 0,
                        overflow: 'hidden',
                        borderRadius: 6,
                        border: `1px solid var(--mantine-color-${active ? 'blue-6' : 'dark-5'})`,
                        backgroundColor: active ? 'var(--mantine-color-blue-light)' : undefined,
                      }}
                    >
                      <Group justify="space-between" wrap="nowrap" gap="xs">
                        <Text size="sm" fw={600} truncate title={style.style_name} style={{ minWidth: 0 }}>{style.style_name}</Text>
                        <Group gap={4} wrap="nowrap">
                          <Badge size="xs" variant="light" color="gray" title="Files using this style">
                            {style.file_count} {style.file_count === 1 ? 'file' : 'files'}
                          </Badge>
                          <Badge size="xs" variant="light" color="gray" title="Subtitle events using this style across those files">
                            {style.event_count} {style.event_count === 1 ? 'event' : 'events'}
                          </Badge>
                        </Group>
                      </Group>
                      <Text size="xs" c="dimmed" truncate title={fontSummary(style)}>{fontSummary(style)}</Text>
                    </UnstyledButton>
                  );
                })}
                {filtered.length === 0 && <Text size="sm" c="dimmed">No matching styles.</Text>}
              </Stack>
            </Box>
          </Stack>

          <Box style={{ flex: 1, minWidth: 380, minHeight: 0 }}>
            {selected && <StyleEditor key={selected.id} projectId={projectId} style={selected} />}
          </Box>
        </Group>
      )}
    </Modal>
  );
}
