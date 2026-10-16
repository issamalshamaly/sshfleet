import { ActionIcon, Box, Group, Menu, NavLink, Stack, Text, Tooltip, UnstyledButton } from "@mantine/core";
import {
  IconActivity,
  IconArrowsExchange,
  IconCheck,
  IconChevronDown,
  IconFileText,
  IconFolders,
  IconHistory,
  IconPlayerPause,
  IconServer,
  IconSettings,
  IconTerminal2,
  IconX,
} from "@tabler/icons-react";
import { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { Environment } from "../../api/types";
import { useConfig, useFleet } from "../../hooks/useFleet";
import { useEnv } from "../../state/env";
import { useTerminals } from "../../state/terminals";
import { useViewStore } from "../../state/views";
import { envColor } from "../../theme";

// The sidebar's parts. The desktop sidebar, the tablet's drawer and the phone's sheets all draw
// these, so a project, an environment or a view is picked the same way on every screen. None of
// them holds state of its own, which is what makes drawing them in two places safe.

export const NAV = [
  { to: "/fleet", label: "Fleet", icon: IconServer },
  { to: "/logs", label: "Logs", icon: IconFileText },
  { to: "/files", label: "Files", icon: IconFolders },
  { to: "/tunnels", label: "Tunnels", icon: IconArrowsExchange },
  { to: "/audit", label: "Audit", icon: IconHistory },
  { to: "/settings", label: "Settings", icon: IconSettings },
];

/** Beside a project's name wherever it is picked: nothing in it is being watched. */
export function ProjectPausedMark() {
  return (
    <Tooltip label="Paused — sshfleet makes no connection to any of its hosts">
      <IconPlayerPause size={13} color="var(--mantine-color-orange-5)" style={{ flexShrink: 0 }} />
    </Tooltip>
  );
}

export function SectionLabel({ children, mt }: { children: string; mt?: string }) {
  return (
    <Text size="xs" tt="uppercase" fw={700} c="dimmed" px="xs" mt={mt} mb={6} style={{ letterSpacing: 0.6 }}>
      {children}
    </Text>
  );
}

/** The project in view, and the menu that changes it when there is more than one. */
export function ProjectPicker({ onPick }: { onPick?: () => void }) {
  const navigate = useNavigate();
  const config = useConfig();
  const { project, setProject } = useEnv();
  const projects = config.data?.projects ?? [];
  const currentProject = projects.find((p) => p.name === project) ?? null;
  // The fleet is organised project first, then the environments inside it, so the sidebar reads
  // the same way: pick the project, then look at all of it or at one environment of it.
  return (
    <>
      <SectionLabel>Project</SectionLabel>
      {projects.length < 2 ? (
        <Group gap={8} wrap="nowrap" style={{ padding: "8px 10px", borderRadius: 8, background: "var(--mantine-color-dark-6)" }}>
          <Box
            w={9}
            h={9}
            style={{ borderRadius: 5, flexShrink: 0, background: `var(--mantine-color-${envColor(currentProject?.color ?? "gray")}-6)` }}
          />
          <Text size="sm" fw={600} truncate>
            {currentProject?.name ?? "No project"}
          </Text>
          {currentProject?.paused && <ProjectPausedMark />}
        </Group>
      ) : (
        <Menu position="bottom-start" width="target" withinPortal>
          <Menu.Target>
            <UnstyledButton
              style={{
                width: "100%",
                padding: "8px 10px",
                borderRadius: 8,
                background: "var(--mantine-color-dark-6)",
                border: "1px solid var(--mantine-color-dark-4)",
              }}
            >
              <Group gap={8} wrap="nowrap" justify="space-between">
                <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
                  <Box
                    w={9}
                    h={9}
                    style={{ borderRadius: 5, flexShrink: 0, background: `var(--mantine-color-${envColor(currentProject?.color ?? "gray")}-6)` }}
                  />
                  <Text size="sm" fw={600} truncate>
                    {currentProject?.name ?? "No project"}
                  </Text>
                  {currentProject?.paused && <ProjectPausedMark />}
                </Group>
                <IconChevronDown size={14} style={{ opacity: 0.6, flexShrink: 0 }} />
              </Group>
            </UnstyledButton>
          </Menu.Target>
          <Menu.Dropdown miw={248}>
            <Menu.Label>Projects</Menu.Label>
            {projects.map((item) => (
              <Menu.Item
                key={item.name}
                onClick={() => setProject(item.name)}
                leftSection={<Box w={9} h={9} style={{ borderRadius: 5, background: `var(--mantine-color-${envColor(item.color)}-6)` }} />}
                rightSection={item.name === project ? <IconCheck size={14} style={{ opacity: 0.7 }} /> : undefined}
              >
                <Group gap={6} wrap="nowrap">
                  <Text size="sm" truncate>
                    {item.name}
                  </Text>
                  {item.paused && <ProjectPausedMark />}
                </Group>
              </Menu.Item>
            ))}
            <Menu.Divider />
            <Menu.Item
              leftSection={<IconSettings size={14} />}
              onClick={() => {
                navigate("/settings?tab=projects");
                onPick?.();
              }}
            >
              Manage projects
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      )}
    </>
  );
}

/** "All environments" and one line per environment of the project, each with how it is doing. */
export function EnvironmentList({ onPick }: { onPick?: () => void }) {
  const config = useConfig();
  const fleet = useFleet();
  const { env, setEnv, project, envNames } = useEnv();
  const currentProject = (config.data?.projects ?? []).find((p) => p.name === project) ?? null;
  const projectEnvironments = useMemo(
    () => (config.data?.environments ?? []).filter((e) => e.project === project),
    [config.data, project],
  );
  const pick = (name: string | null) => {
    setEnv(name);
    onPick?.();
  };

  const renderEnvironmentLink = (environment: Environment) => {
    const all = fleet.data?.hosts.filter((h) => h.env === environment.name) ?? [];
    // "down" is measured over the hosts being watched; a paused one is neither up nor down.
    const hosts = all.filter((h) => !h.paused_by);
    const down = hosts.filter((h) => h.snapshot && !h.snapshot.reachable).length;
    const bastionsDown = environment.bastions.some((b) => fleet.data?.bastions[b] === false);
    // Paused on its own or with its whole project: either way nothing in it is down, only unwatched.
    const paused = environment.paused || !!currentProject?.paused;
    return (
      <NavLink
        key={environment.name}
        label={
          <Group gap={8} wrap="nowrap">
            <Box w={8} h={8} style={{ borderRadius: 4, background: `var(--mantine-color-${envColor(environment.color)}-6)` }} />
            <Text size="sm" tt="capitalize">
              {environment.name}
            </Text>
          </Group>
        }
        description={
          paused ? (
            <Group gap={4} wrap="nowrap">
              <IconPlayerPause size={11} color="var(--mantine-color-orange-5)" />
              <Text size="xs" c="orange.4">
                paused · {all.length} {all.length === 1 ? "host" : "hosts"}
              </Text>
            </Group>
          ) : bastionsDown ? (
            <Text size="xs" c="red.4">
              bastion unreachable
            </Text>
          ) : down ? (
            <Text size="xs" c="yellow.4">
              {down} of {hosts.length} down
            </Text>
          ) : (
            <Text size="xs" c="dimmed">
              {all.length} hosts
            </Text>
          )
        }
        active={env === environment.name}
        onClick={() => pick(environment.name)}
        variant="light"
      />
    );
  };

  // Choosing here only changes what is in view; it never navigates. This is the only environment
  // control in the app, so Settings depends on it to filter its own tables — jumping to the fleet
  // would take that away.
  return (
    <>
      <SectionLabel mt="md">Environments</SectionLabel>
      <Stack gap={2}>
        <NavLink
          label="All environments"
          active={env === null}
          onClick={() => pick(null)}
          leftSection={<IconActivity size={16} />}
          rightSection={
            <Text size="xs" c="dimmed">
              {fleet.data ? fleet.data.hosts.filter((h) => envNames.includes(h.env)).length : "…"}
            </Text>
          }
          variant="light"
        />
        {projectEnvironments.map(renderEnvironmentLink)}
      </Stack>
    </>
  );
}

/** The pages, each brought back at the address it was left on. */
export function ViewList({ onPick, items = NAV, label = "Views" }: { onPick?: () => void; items?: typeof NAV; label?: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const views = useViewStore();
  return (
    <>
      <SectionLabel>{label}</SectionLabel>
      <Stack gap={2}>
        {items.map((item) => (
          <NavLink
            key={item.to}
            label={item.label}
            leftSection={<item.icon size={17} />}
            active={location.pathname.startsWith(item.to)}
            // A view is a place you go back to, at the address it was left on: the tab of
            // Settings, the directory in Files, the host open on the fleet. Logs comes back to
            // its search, results and all. Only an icon beside a host starts anew.
            onClick={() => {
              navigate(views.resume(item.to));
              onPick?.();
            }}
            variant="light"
          />
        ))}
      </Stack>
    </>
  );
}

/** How many of the hosts in view hold a connection, and the version. */
export function NavFooter() {
  const config = useConfig();
  const fleet = useFleet();
  const { inScope } = useEnv();
  // A paused host holds no connection on purpose, so it is not one the count is missing.
  const inView = useMemo(() => (fleet.data?.hosts ?? []).filter((h) => inScope(h.env) && !h.paused_by), [fleet.data, inScope]);
  return (
    <Group justify="space-between" px="xs" pt="sm" style={{ borderTop: "1px solid var(--mantine-color-dark-5)" }}>
      <Group gap={6}>
        <IconTerminal2 size={14} style={{ opacity: 0.6 }} />
        <Text size="xs" c="dimmed">
          connections {inView.filter((h) => h.connected).length}/{inView.length}
        </Text>
      </Group>
      <Text size="xs" c="dimmed">
        v{config.data?.version ?? ""}
      </Text>
    </Group>
  );
}

/** Open shells as a list, for the phone, where there is no header menu to keep them in. A tap
 *  brings one back to the front; the cross ends it. */
export function ShellList({ onPick }: { onPick?: () => void }) {
  const terminals = useTerminals();
  if (terminals.sessions.length === 0) return null;
  return (
    <>
      <SectionLabel mt="md">Open shells</SectionLabel>
      <Stack gap={2}>
        {terminals.sessions.map((session) => (
          <NavLink
            key={session.id}
            onClick={() => {
              terminals.show(session.id);
              onPick?.();
            }}
            leftSection={<IconTerminal2 size={16} color={terminals.ended(session.id) ? undefined : "var(--mantine-color-teal-4)"} />}
            label={
              <Text size="sm" className="mono" truncate>
                {session.title}
              </Text>
            }
            description={terminals.ended(session.id) ? "ended" : (session.subtitle ?? session.alias)}
            rightSection={
              <ActionIcon
                variant="subtle"
                color="gray"
                component="span"
                aria-label={`close ${session.title}`}
                onClick={(event) => {
                  event.stopPropagation();
                  terminals.close(session.id);
                }}
              >
                <IconX size={15} />
              </ActionIcon>
            }
          />
        ))}
      </Stack>
    </>
  );
}
