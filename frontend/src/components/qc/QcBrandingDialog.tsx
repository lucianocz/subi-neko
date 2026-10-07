import { useState } from 'react';
import { Alert, Button, Group, Modal, Select, Stack, Switch, Text, TextInput } from '@mantine/core';
import type { QcBranding, QcBrandingSave } from '../../types/qc';
import {
  draftFromBranding, isBrandingDirty, parseBrandingDraft, templateOptions,
} from '../../utils/qcBranding';
import type { BrandingDraft } from '../../utils/qcBranding';
import { formatTimeField } from '../../utils/qcTime';

interface Props {
  opened: boolean;
  onClose: () => void;
  config: QcBranding;
  templates: string[] | undefined;
  templatesError: boolean;
  /** Player clock in ms (null when no video element). */
  getVideoTimeMs: () => number | null;
  /** Persist; throws a displayable Error on failure. Closing is the dialog's job. */
  onSave: (body: QcBrandingSave) => Promise<void>;
}

/** Modal with a local draft: nothing is saved until Save, Cancel discards. */
export function QcBrandingDialog({ opened, onClose, ...rest }: Props) {
  // Children unmount on close, so every open starts from the saved config.
  return (
    <Modal opened={opened} onClose={onClose} title="Branding" centered size="md" data-testid="qc-branding-dialog">
      <BrandingForm onClose={onClose} {...rest} />
    </Modal>
  );
}

function BrandingForm({
  onClose, config, templates, templatesError, getVideoTimeMs, onSave,
}: Omit<Props, 'opened'>) {
  const [draft, setDraft] = useState<BrandingDraft>(() => draftFromBranding(config));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const options = templateOptions(templates ?? [], draft.template);
  const dirty = isBrandingDirty(draft, config);

  const patch = (p: Partial<BrandingDraft>) => { setDraft((d) => ({ ...d, ...p })); setError(null); };

  const setToCurrent = () => {
    const ms = getVideoTimeMs();
    if (ms !== null) patch({ start: formatTimeField(ms) });
  };

  const submit = async () => {
    const parsed = parseBrandingDraft(draft);
    if (!parsed.ok) { setError(parsed.reason); return; }
    setSaving(true);
    try {
      await onSave(parsed.body);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save branding.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Stack gap="sm">
      <Switch
        label="Enabled"
        checked={draft.enabled}
        onChange={(e) => patch({ enabled: e.currentTarget.checked })}
        data-testid="qc-branding-enabled"
      />
      <Select
        label="Template"
        placeholder={options.length === 0 ? 'No templates in config/brand' : 'Choose a template'}
        data={options}
        value={draft.template}
        onChange={(v) => patch({ template: v })}
        allowDeselect={false}
        disabled={options.length === 0}
        data-testid="qc-branding-template"
      />
      {templatesError && <Text size="xs" c="red">Could not load the template list.</Text>}
      <Group align="flex-end" gap="xs" wrap="nowrap">
        <TextInput
          label="Start"
          value={draft.start}
          onChange={(e) => patch({ start: e.currentTarget.value })}
          placeholder="HH:MM:SS.mmm"
          style={{ flex: 1 }}
          data-testid="qc-branding-start"
        />
        <Button variant="default" onClick={setToCurrent} data-testid="qc-branding-current">Set to current</Button>
      </Group>
      <Text size="xs" c="dimmed">
        Shifts the whole template: each of its events starts this long after its own template time.
      </Text>
      {error && <Alert color="red" p="xs" data-testid="qc-branding-error">{error}</Alert>}
      <Group justify="flex-end" gap="xs">
        <Button variant="default" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button onClick={() => { void submit(); }} loading={saving} disabled={!dirty} data-testid="qc-branding-save">
          Save
        </Button>
      </Group>
    </Stack>
  );
}
