import { Box, Button, Collapse, Drawer, Group, Menu, Stack, Text, UnstyledButton } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { IconChevronDown, IconChevronRight, IconEyeOff } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { Alert } from "../api/types";
import { useViewState } from "../state/views";
import { EmptyState, SeverityBadge } from "./common";

/** The glob the server saves for "every container named like it" (inventory/model.py
 *  `ignore_glob_for`): the name up to its first numbered segment. Shown before choosing, so
 *  nobody has to guess what a family rule will silence. */
export function familyGlob(name: string): string {
  const match = /[-_][^-_]*\d[^-_]*/.exec(name);
  if (!match || match.index === 0) return name;
  return `${name.slice(0, match.index + 1)}*`;
}

/** Only alerts about a container that could be ignored: one that is gone, or one that stopped. */
const IGNORABLE = new Set(["ABSENT", "STOPPED"]);

function IgnoreMenu({ alert }: { alert: Alert }) {
  const client = useQueryClient();
  const name = alert.container!;
  const family = familyGlob(name);
  const ignore = (scope: "name" | "family") =>
    api
      .ignoreContainer(alert.host, name, scope)
      .then(({ pattern }) => {
        void client.invalidateQueries({ queryKey: ["fleet"] });
        void client.invalidateQueries({ queryKey: ["config"] });
        notifications.show({
          title: `${pattern} ignored on ${alert.host}`,
          message:
            scope === "family"
              ? "No container matching it raises an alert on this host any more. Undo it from History."
              : "It is no longer expected on this host. Undo it from History.",
          color: "gray",
        });
      })
      .catch((error: Error) => notifications.show({ title: "Could not save", message: error.message, color: "red" }));

  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          leftSection={<IconEyeOff size={13} />}
          rightSection={<IconChevronDown size={11} />}
          onClick={(event) => event.stopPropagation()}
        >
          Ignore
        </Button>
      </Menu.Target>
      <Menu.Dropdown onClick={(event) => event.stopPropagation()}>
        <Menu.Item onClick={() => void ignore("name")}>
          This container
          <Text size="xs" c="dimmed">
            It {alert.code === "ABSENT" ? "left" : "stopped"} on purpose: stop alerting on it here.
          </Text>
        </Menu.Item>
        {family !== name && (
          // The one that matters for a scheduler that names a container per run: ignoring
          // this one name would silence nothing next time.
          <Menu.Item onClick={() => void ignore("family")}>
            Every container named like it
            <Text size="xs" c="dimmed" className="mono">
              {family}
            </Text>
          </Menu.Item>
        )}
      </Menu.Dropdown>
    </Menu>
  );
}

function AlertCard({ alert, onOpen }: { alert: Alert; onOpen: () => void }) {
  // A focusable div, not a button: the Ignore menu inside it is a button, and buttons do not nest.
  return (
    <Box
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" && event.target === event.currentTarget) onOpen();
      }}
      p="sm"
      className="clickable-card"
      style={{ border: "1px solid var(--mantine-color-dark-5)", borderRadius: 8, cursor: "pointer" }}
    >
      <Group justify="space-between" wrap="nowrap" mb={4}>
        <SeverityBadge severity={alert.severity}>{alert.code}</SeverityBadge>
        <Text size="xs" c="dimmed" className="mono">
          {alert.host}
        </Text>
      </Group>
      <Group justify="space-between" wrap="nowrap" align="flex-start" gap="xs">
        <Text size="sm" style={{ minWidth: 0 }}>
          {alert.message}
        </Text>
        {alert.container && IGNORABLE.has(alert.code) && <IgnoreMenu alert={alert} />}
      </Group>
    </Box>
  );
}

/**
 * What needs attention, and apart from it what is only worth knowing.
 *
 * The fleet's Alerts card and the bell count critical and warning; the drawer used to count every
 * entry, the informational ones too, so it said 16 where the card said 8. Notes — a container that
 * exited cleanly before sshfleet first saw it, a restart count from more than a day ago — are now
 * listed below, folded, and counted separately, so every number on the page agrees.
 */
export function AlertsDrawer({ opened, onClose, alerts }: { opened: boolean; onClose: () => void; alerts: Alert[] }) {
  const navigate = useNavigate();
  const [notesOpen, setNotesOpen] = useViewState("alerts.notesOpen", false);
  const order = { crit: 0, warn: 1, info: 2 };
  const sorted = [...alerts].sort((a, b) => order[a.severity] - order[b.severity] || a.host.localeCompare(b.host));
  const active = sorted.filter((a) => a.severity !== "info");
  const notes = sorted.filter((a) => a.severity === "info");
  const open = (alert: Alert) => {
    onClose();
    navigate(`/fleet?host=${encodeURIComponent(alert.host)}`);
  };

  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="md" title={`Active alerts (${active.length})`}>
      <Stack gap="xs">
        {active.length === 0 ? (
          <EmptyState title="Nothing needs attention" hint="Alerts appear here as soon as a refresh finds one." />
        ) : (
          active.map((alert) => <AlertCard key={alert.key} alert={alert} onOpen={() => open(alert)} />)
        )}
        {notes.length > 0 && (
          <>
            <UnstyledButton onClick={() => setNotesOpen((current) => !current)} mt="sm" py={4}>
              <Group gap={6}>
                {notesOpen ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
                <Text size="sm" fw={600}>
                  Notes ({notes.length})
                </Text>
                <Text size="xs" c="dimmed">
                  worth knowing, nothing wrong
                </Text>
              </Group>
            </UnstyledButton>
            <Collapse in={notesOpen}>
              <Stack gap="xs">
                <Text size="xs" c="dimmed">
                  Containers that exited cleanly before sshfleet first saw them, and restart counts from runs more than a day old. They
                  are not counted as alerts.
                </Text>
                {notes.map((alert) => (
                  <AlertCard key={alert.key} alert={alert} onOpen={() => open(alert)} />
                ))}
              </Stack>
            </Collapse>
          </>
        )}
      </Stack>
    </Drawer>
  );
}
