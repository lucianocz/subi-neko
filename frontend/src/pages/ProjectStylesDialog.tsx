import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  Paper,
  Stack,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { TextAa } from '@phosphor-icons/react';
import { StylePropertyGrid } from '../components/Project/StylePropertyGrid';
import { FONT_PREVIEW_LINES } from '../constants/replacementFonts';
import { useProjectStyles, useUpdateProjectStyle } from '../hooks/useProjectStyles';
import type { ProjectStyle } from '../hooks/useProjectStyles';
import {
  draftFromStyle,
  draftToUpdate,
  formatDecimal,
  hasAnyOverride,
  hasStoredOverride,
  isDraftDirty,
  isDraftValid,
  previewCss,
  resolveEffective,
} from '../utils/styleOverrides.ts';
import type { StyleDraft } from '../utils/styleOverrides.ts';

interface ProjectStylesDialogProps {
  projectId: number;
  opened: boolean;
  onClose: () => void;
}

/** "Arial 42 → Noto Sans 40"; falls back to the source value per field. */
function fontSummary(style: ProjectStyle) {
  const source = `${style.font_name} ${formatDecimal(style.font_size)}`;
  const restyled = hasStoredOverride(style);
  if (!restyled) return source;
  const name = style.replacement_font_name || style.font_name;
  const size = style.replacement_font_size ?? style.font_size;
  const fontChanged = Boolean(style.replacement_font_name) || style.replacement_font_size != null;
  return fontChanged ? `${source} → ${name} ${formatDecimal(size)} · restyled` : `${source} · restyled`;
}

function StyleEditor({ projectId, style }: { projectId: number; style: ProjectStyle }) {
  const update = useUpdateProjectStyle(projectId);
  // Edits stay local until Save (one PUT), like the font/size fields always did.
  const [draft, setDraft] = useState<StyleDraft>(() => draftFromStyle(style));
  const patch = useCallback((p: Partial<StyleDraft>) => setDraft((d) => ({ ...d, ...p })), []);

  // Same fallback semantics as translated ASS export (backend `effective_style`).
  const effective = resolveEffective(style, draft);
  const css = previewCss(effective);
  const dirty = isDraftDirty(draft, style);
  const valid = isDraftValid(draft);

  async function save() {
    try {
      await update.mutateAsync({ styleId: style.id, ...draftToUpdate(draft) });
      notifications.show({
        color: 'green',
        message: `Style "${style.style_name}" updated for ${style.file_count} file${style.file_count === 1 ? '' : 's'}.`,
      });
    } catch {
      notifications.show({ color: 'red', title: 'Save failed', message: 'Could not update the style.' });
    }
  }

  return (
    <Stack gap="sm" h="100%" style={{ minHeight: 0 }}>
      <Text size="xs" c="dimmed" style={{ flex: '0 0 auto' }}>
        Replacements apply to translated subtitles in all {style.file_count} file
        {style.file_count === 1 ? '' : 's'} using this style. Source subtitles always keep the original style.
        Empty / dashed controls inherit the source value; the &times; or &ldquo;Reset to source&rdquo; clears one override.
      </Text>

      <Box style={{ flex: '0 0 auto' }}>
        <StylePropertyGrid style={style} draft={draft} onPatch={patch} />
      </Box>

      {/* Bounded: the sample keeps its real size and scrolls here instead of growing the dialog. */}
      <Paper
        withBorder
        p="md"
        radius="sm"
        style={{ flex: '1 1 0', minHeight: 80, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
      >
        <Group justify="space-between" mb="xs" style={{ flex: '0 0 auto' }}>
          <Text size="xs" c="dimmed">Preview (translated, approximate)</Text>
          <Badge size="sm" variant="light" tt="none">{effective.fontName} {formatDecimal(effective.fontSize)}</Badge>
        </Group>
        <Box
          className="sg-preview-stage"
          p="sm"
          style={{
            flex: '1 1 0',
            minHeight: 0,
            overflow: 'auto',
            overflowWrap: 'anywhere',
            borderRadius: 4,
            lineHeight: 1.3,
            ...css,
          }}
        >
          {FONT_PREVIEW_LINES.map((line) => <div key={line}>{line}</div>)}
        </Box>
      </Paper>

      <Group justify="flex-end" style={{ flex: '0 0 auto' }}>
        <Button
          variant="subtle"
          color="gray"
          disabled={!hasAnyOverride(draft)}
          onClick={() => setDraft(draftFromStyle({
            ...style,
            replacement_font_name: null, replacement_font_size: null, replacement_bold: null,
            replacement_italic: null, replacement_outline: null, replacement_shadow: null,
            replacement_primary_colour: null, replacement_outline_colour: null, replacement_back_colour: null,
          }))}
        >
          Use source style
        </Button>
        <Button onClick={() => void save()} disabled={!dirty || !valid} loading={update.isPending}>
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
      size="min(1280px, 96vw)"
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
          <Stack gap="xs" w={300} miw={260} maw={300} style={{ flexShrink: 0, minHeight: 0 }}>
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

          <Box style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
            {selected && <StyleEditor key={selected.id} projectId={projectId} style={selected} />}
          </Box>
        </Group>
      )}
    </Modal>
  );
}
