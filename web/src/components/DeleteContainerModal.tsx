import { Alert, Badge, Button, Checkbox, Code, Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation } from "@tanstack/react-query";
import { IconAlertTriangle, IconTrash } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { Container } from "../api/types";

interface Props {
  alias: string;
  env: string;
  container: Container | null;
  onClose: () => void;
}

/**
 * Remove a container that is already stopped.
 *
 * The only action in the app that leaves no trace on the server, so it asks the same thing on
 * every environment: type the name. The container's log goes with it, which is why the last
 * lines are saved first unless you untick that.
 */
export function DeleteContainerModal({ alias, env, container, onClose }: Props) {
  const opened = container !== null;
  const [confirm, setConfirm] = useState("");
  const [saveLogs, setSaveLogs] = useState(true);

  useEffect(() => {
    setConfirm("");
    setSaveLogs(true);
  }, [container]);

  const remove = useMutation({
    mutationFn: () => api.removeContainer(alias, container!.name, confirm, saveLogs),
    onSuccess: (result) => {
      notifications.show({
        title: `${container?.name} removed`,
        message: result.saved_log ? `Its last log lines are in ${result.saved_log}` : "Its log went with it, as you asked.",
        color: "teal",
        autoClose: 10_000,
      });
      onClose();
    },
    onError: (error) =>
      notifications.show({ title: "Not removed", message: String((error as Error).message), color: "red", autoClose: 8000 }),
  });

  const exitNote = container?.exit_code === null || container?.exit_code === undefined ? "" : ` (exit ${container.exit_code})`;
  const canRun = opened && confirm.trim() === container?.name && !remove.isPending;

  return (
    <Modal opened={opened} onClose={onClose} title="Remove a stopped container" centered>
      {container && (
        <Stack gap="sm">
          <Group gap="xs">
            <Text size="sm" className="mono" fw={700}>
              {container.name}
            </Text>
            <Text size="sm" c="dimmed">
              on
            </Text>
            <Text size="sm" className="mono">
              {alias}
            </Text>
            <Badge size="sm" variant="light" color="gray">
              {env}
            </Badge>
            <Badge size="sm" variant="light" color={container.exit_code ? "red" : "gray"}>
              {container.state}
              {exitNote}
            </Badge>
          </Group>
          <Text size="sm" c="dimmed">
            {container.status}
          </Text>
          <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />}>
            Removing a container deletes its log with it. This is checked again on the host right before it runs: a container
            that has started since you opened this window is left alone.
          </Alert>
          <Checkbox
            label="Save its last 2,000 log lines to the downloads folder first"
            checked={saveLogs}
            onChange={(e) => setSaveLogs(e.currentTarget.checked)}
          />
          <Code block>{`${container.name} → rm`}</Code>
          <TextInput
            label="Type the container name to confirm"
            placeholder={container.name}
            value={confirm}
            onChange={(e) => setConfirm(e.currentTarget.value)}
            className="mono"
            data-autofocus
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button color="red" leftSection={<IconTrash size={16} />} disabled={!canRun} loading={remove.isPending} onClick={() => remove.mutate()}>
              Remove
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
