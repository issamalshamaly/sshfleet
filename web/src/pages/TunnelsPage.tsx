import { ActionIcon, Anchor, Badge, Button, Card, Group, Loader, Modal, Stack, Switch, Table, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconExternalLink, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { Tunnel } from "../api/types";
import TunnelModal from "../components/TunnelModal";
import { EmptyState, EnvBadge } from "../components/common";
import { useConfig, useFleet } from "../hooks/useFleet";
import { fmtTime } from "../lib/format";
import { useEnv } from "../state/env";

/** What is really on the port, which the switch alone (the operator's intent) cannot say. */
function TunnelState({ tunnel }: { tunnel: Tunnel }) {
  if (tunnel.up) {
    return (
      <Badge color="teal" variant="light" size="xs">
        open
      </Badge>
    );
  }
  if (tunnel.dropped) {
    const detail = tunnel.attempts
      ? `Could not reopen it yet (${tunnel.attempts} ${tunnel.attempts === 1 ? "try" : "tries"})${tunnel.error ? ` — ${tunnel.error}` : ""}. Next try at ${fmtTime(tunnel.retry_at)}.`
      : `Its connection to ${tunnel.via} went. sshfleet is opening it again.`;
    return (
      <Tooltip label={`${detail} Turn it off to stop.`} multiline maw={360}>
        <Badge color="yellow" variant="light" size="xs" leftSection={<Loader size={8} color="yellow" />}>
          reconnecting
        </Badge>
      </Tooltip>
    );
  }
  if (tunnel.held) {
    return (
      <Tooltip
        label={`The tunnel is closed, but something still listens on ${tunnel.endpoint}. Opening it will pick the next free port.`}
        multiline
        maw={360}
      >
        <Badge color="orange" variant="light" size="xs">
          port busy
        </Badge>
      </Tooltip>
    );
  }
  return null;
}

export default function TunnelsPage() {
  const { env, envNames, inScope } = useEnv();
  const config = useConfig();
  // Only this project's environments: another project may reuse the same names. Memoised because
  // the modal resets its form whenever this list changes identity.
  const environments = useMemo(
    () => (config.data?.environments ?? []).filter((e) => envNames.includes(e.name)),
    [config.data, envNames],
  );
  const fleet = useFleet();
  const client = useQueryClient();
  const tunnels = useQuery({ queryKey: ["tunnels"], queryFn: api.tunnels.list, refetchInterval: 15_000 });
  const [modal, setModal] = useState<{ open: boolean; editing: Tunnel | null }>({ open: false, editing: null });
  const [removing, setRemoving] = useState<Tunnel | null>(null);

  const remove = useMutation({
    mutationFn: (tunnel: Tunnel) => api.settings.removeTunnel(tunnel.name),
    onSuccess: (_result, tunnel) => {
      void client.invalidateQueries({ queryKey: ["tunnels"] });
      setRemoving(null);
      notifications.show({ title: `${tunnel.name} deleted`, message: "", color: "gray" });
    },
    onError: (error) => notifications.show({ title: "Could not delete", message: String((error as Error).message), color: "red" }),
  });

  const toggle = useMutation({
    mutationFn: ({ name, up }: { name: string; up: boolean }) => (up ? api.tunnels.up(name) : api.tunnels.down(name)),
    onSuccess: (tunnel) => {
      void client.invalidateQueries({ queryKey: ["tunnels"] });
      notifications.show({
        title: tunnel.up ? `${tunnel.name} is open` : `${tunnel.name} closed`,
        message: tunnel.up ? `${tunnel.endpoint} → ${tunnel.remote} via ${tunnel.via}` : "",
        color: tunnel.up ? "teal" : "gray",
      });
    },
    onError: (error, { name, up }) => {
      // A close can fail half-way: sshfleet let go, the port did not. Show the row as it is now.
      void client.invalidateQueries({ queryKey: ["tunnels"] });
      notifications.show({
        title: up ? `Could not open ${name}` : `${name} closed, but its port is still open`,
        message: String((error as Error).message),
        color: "red",
        autoClose: up ? 8000 : false,
      });
    },
  });

  const rows = (tunnels.data ?? []).filter((t) => inScope(t.env));

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start">
        <div>
          <Text fw={700} size="lg">
            Tunnels
          </Text>
          <Text size="sm" c="dimmed" maw={680}>
            Port forwards to Kibana, Seq, Elasticsearch or a database. They ride on the connections sshfleet already has open, so
            opening one costs nothing extra. If a connection drops, sshfleet opens its tunnels again until you turn them off.
          </Text>
        </div>
        <Button leftSection={<IconPlus size={16} />} onClick={() => setModal({ open: true, editing: null })}>
          Add tunnel
        </Button>
      </Group>
      <Card withBorder padding={0}>
        {rows.length === 0 ? (
          <EmptyState title="No tunnels yet" hint="Add one to reach Kibana, Seq or a database through a bastion." />
        ) : (
          <Table verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Tunnel</Table.Th>
                <Table.Th>Env</Table.Th>
                <Table.Th>Local</Table.Th>
                <Table.Th>Remote</Table.Th>
                <Table.Th>Via</Table.Th>
                <Table.Th>Open</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((tunnel) => (
                <Table.Tr key={tunnel.name}>
                  <Table.Td>
                    <Group gap="sm">
                      <Switch
                        checked={tunnel.wanted}
                        onChange={(e) => toggle.mutate({ name: tunnel.name, up: e.currentTarget.checked })}
                        disabled={toggle.isPending}
                        color="teal"
                      />
                      <Text fw={600} className="mono">
                        {tunnel.name}
                      </Text>
                      <TunnelState tunnel={tunnel} />
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <EnvBadge env={tunnel.env} environments={config.data?.environments} />
                  </Table.Td>
                  <Table.Td>
                    <Text className="mono" size="sm">
                      {tunnel.endpoint}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text className="mono" size="sm" c="dimmed">
                      {tunnel.remote}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text className="mono" size="sm" c="dimmed">
                      {tunnel.via}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {tunnel.url ? (
                      <Anchor href={tunnel.url} target="_blank" size="sm" className="mono">
                        {tunnel.url}
                      </Anchor>
                    ) : (
                      <Text size="sm" c="dimmed">
                        —
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4} justify="flex-end" wrap="nowrap">
                      {tunnel.url && (
                        <Button
                          size="xs"
                          variant="default"
                          component="a"
                          href={tunnel.url}
                          target="_blank"
                          disabled={!tunnel.up}
                          rightSection={<IconExternalLink size={14} />}
                        >
                          Open
                        </Button>
                      )}
                      <Tooltip label={tunnel.wanted ? "Close it before changing it" : "Edit"}>
                        <ActionIcon variant="subtle" color="gray" disabled={tunnel.wanted} onClick={() => setModal({ open: true, editing: tunnel })}>
                          <IconPencil size={15} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label={tunnel.wanted ? "Close it before deleting it" : "Delete"}>
                        <ActionIcon variant="subtle" color="red" disabled={tunnel.wanted} onClick={() => setRemoving(tunnel)}>
                          <IconTrash size={15} />
                        </ActionIcon>
                      </Tooltip>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>

      <TunnelModal
        opened={modal.open}
        editing={modal.editing}
        hosts={fleet.data?.hosts ?? []}
        environments={environments}
        defaultEnv={env}
        onClose={() => setModal({ open: false, editing: null })}
      />

      <Modal opened={!!removing} onClose={() => setRemoving(null)} title={`Delete ${removing?.name}?`} centered>
        <Stack gap="sm">
          <Text size="sm">
            The forward definition is removed from your settings. Nothing on the servers changes.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button color="red" loading={remove.isPending} onClick={() => removing && remove.mutate(removing)}>
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
