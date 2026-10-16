import { ActionIcon, Badge, Button, Code, Group, Loader, Menu, Stack, Text, Tooltip } from "@mantine/core";
import { IconBolt, IconEye, IconPlayerPlay, IconTerminal2, IconTool } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { QuickAction } from "../api/types";
import { useNoHover } from "../lib/responsive";
import { useTerminals } from "../state/terminals";
import { BottomSheet, DataCard } from "./responsive";

const GROUPS: { key: string; label: string; icon: typeof IconTerminal2 }[] = [
  { key: "shell", label: "Get inside", icon: IconTerminal2 },
  { key: "toolkit", label: "Tools in this image", icon: IconTool },
  { key: "watch", label: "Watch it work", icon: IconEye },
];

/**
 * The things an operator actually does to one container, one click away: a shell inside it, a
 * root shell, the client its image ships with, its log as it is written.
 *
 * The list is the server's, not this component's. Which actions make sense depends on the
 * image, the container's state, the host's role and whether a health endpoint is configured —
 * all of which the server already knows — and the command each one runs is built there too.
 * Here it is only drawn, with the exact line on hover so nothing runs unread. A touch screen has
 * no hover, so there the list is a sheet that shows each line beside its Run button instead.
 */
export function ContainerActions({
  alias,
  container,
  state,
  labelled = false,
}: {
  alias: string;
  container: string;
  state: string;
  /** A worded button, for a card, instead of the bare icon a table row has room for. */
  labelled?: boolean;
}) {
  const [opened, setOpened] = useState(false);
  const noHover = useNoHover();
  const terminals = useTerminals();
  // Asked for when the menu is first opened, not for every container in the table: one request
  // per container on a host with twenty of them would be twenty round trips nobody asked for.
  const actions = useQuery({
    queryKey: ["container-actions", alias, container, state],
    queryFn: () => api.containerActions(alias, container),
    enabled: opened,
    staleTime: 60_000,
  });

  const run = (action: QuickAction) =>
    terminals.openAction({
      alias,
      container,
      action: action.id,
      title: container,
      subtitle: action.title,
    });

  const available = actions.data?.actions ?? [];

  const status = (
    <>
      {actions.isLoading && (
        <Group justify="center" py="sm">
          <Loader size={16} />
        </Group>
      )}
      {actions.isError && (
        <Text size="xs" c="red.4" p="sm">
          {(actions.error as Error).message}
        </Text>
      )}
      {actions.isSuccess && available.length === 0 && (
        <Text size="xs" c="dimmed" p="sm">
          Nothing to offer for this container.
        </Text>
      )}
    </>
  );

  if (noHover) {
    return (
      <>
        {labelled ? (
          <Button variant="default" leftSection={<IconBolt size={16} />} onClick={() => setOpened(true)}>
            Quick actions
          </Button>
        ) : (
          <ActionIcon variant="subtle" color="gray" aria-label={`quick actions for ${container}`} onClick={() => setOpened(true)}>
            <IconBolt size={16} />
          </ActionIcon>
        )}
        <BottomSheet
          opened={opened}
          onClose={() => setOpened(false)}
          title={
            <Text fw={700} className="mono" style={{ overflowWrap: "anywhere" }}>
              Quick actions · {container}
            </Text>
          }
        >
          {status}
          <Stack gap="md">
            {GROUPS.map(({ key, label, icon: Icon }) => {
              const group = available.filter((action) => action.group === key);
              if (!group.length) return null;
              return (
                <Stack key={key} gap="xs">
                  <Text size="xs" tt="uppercase" fw={700} c="dimmed" style={{ letterSpacing: 0.6 }}>
                    {label}
                  </Text>
                  {group.map((action) => (
                    // Nothing runs from a tap on the card: only Run does, with the line it runs in
                    // plain sight above it.
                    <DataCard key={action.id}>
                      <Group gap={6} wrap="nowrap" justify="space-between">
                        <Group gap={8} wrap="nowrap">
                          <Icon size={16} />
                          <Text size="sm" fw={600}>
                            {action.title}
                          </Text>
                        </Group>
                        {action.writes && (
                          <Badge size="xs" variant="outline" color="yellow">
                            installs
                          </Badge>
                        )}
                      </Group>
                      <Text size="xs" c="dimmed" mt={4}>
                        {action.detail}
                      </Text>
                      <Code block mt={6} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                        {action.command}
                      </Code>
                      <Button
                        mt="xs"
                        fullWidth
                        variant="light"
                        leftSection={<IconPlayerPlay size={16} />}
                        onClick={() => {
                          setOpened(false);
                          run(action);
                        }}
                      >
                        Run
                      </Button>
                    </DataCard>
                  ))}
                </Stack>
              );
            })}
          </Stack>
        </BottomSheet>
      </>
    );
  }

  return (
    <Menu position="bottom-end" width={330} withinPortal opened={opened} onChange={setOpened} closeOnItemClick>
      <Menu.Target>
        {labelled ? (
          <Button variant="default" leftSection={<IconBolt size={16} />}>
            Quick actions
          </Button>
        ) : (
          <Tooltip label="Quick actions — a shell inside this container, and the rest" openDelay={300}>
            <ActionIcon variant="subtle" color="gray" aria-label={`quick actions for ${container}`}>
              <IconBolt size={16} />
            </ActionIcon>
          </Tooltip>
        )}
      </Menu.Target>
      <Menu.Dropdown>
        {status}
        {GROUPS.map(({ key, label, icon: Icon }) => {
          const group = available.filter((action) => action.group === key);
          if (!group.length) return null;
          return (
            <div key={key}>
              <Menu.Label>{label}</Menu.Label>
              {group.map((action) => (
                // The command on hover, verbatim: these open a session on a real server, and
                // the one that installs says so on its face rather than in the tooltip alone.
                <Tooltip
                  key={action.id}
                  label={
                    <Stack gap={2}>
                      <Text size="xs">{action.detail}</Text>
                      <Text size="xs" className="mono" c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
                        {action.command}
                      </Text>
                    </Stack>
                  }
                  multiline
                  maw={460}
                  position="left"
                  openDelay={250}
                >
                  <Menu.Item leftSection={<Icon size={14} />} onClick={() => run(action)}>
                    <Group gap={6} wrap="nowrap" justify="space-between">
                      <Text size="sm">{action.title}</Text>
                      {action.writes && (
                        <Badge size="xs" variant="outline" color="yellow">
                          installs
                        </Badge>
                      )}
                    </Group>
                  </Menu.Item>
                </Tooltip>
              ))}
            </div>
          );
        })}
      </Menu.Dropdown>
    </Menu>
  );
}
