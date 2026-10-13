import { Button, Group, Modal, Text } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { useDeleteProject } from '../../hooks/useProjects';
import { apiErrorMessage } from '../../utils/projectArchive';

/** The one project-deletion confirmation, shared by the project header menu and
 * the Archived projects table. */
export function RemoveProjectModal({
  project,
  onClose,
  onRemoved,
}: {
  project: { id: number; name: string } | null;
  onClose: () => void;
  onRemoved?: () => void;
}) {
  const deleteMutation = useDeleteProject();

  async function handleDelete() {
    if (!project) return;
    try {
      await deleteMutation.mutateAsync(project.id);
    } catch (err) {
      notifications.show({
        color: 'red',
        message: apiErrorMessage(err, 'Could not remove the project.'),
      });
      return;
    }
    onClose();
    onRemoved?.();
  }

  return (
    <Modal opened={project !== null} onClose={onClose} title="Remove project" size="sm">
      <Text size="sm" mb="lg">
        Remove <strong>{project?.name}</strong>? All project data (files, jobs, characters) will be
        deleted from the database. Files on disk will not be touched.
      </Text>
      <Group justify="flex-end" gap="sm">
        <Button variant="default" onClick={onClose}>
          Cancel
        </Button>
        <Button color="red" loading={deleteMutation.isPending} onClick={handleDelete}>
          Remove
        </Button>
      </Group>
    </Modal>
  );
}
