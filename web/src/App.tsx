import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Box,
  Group,
  Menu,
  Indicator,
  Loader,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { Spotlight, spotlight, type SpotlightActionData } from "@mantine/spotlight";
import {
  IconActivity,
  IconAlertTriangle,
  IconArrowsExchange,
  IconBell,
  IconCheck,
  IconChevronDown,
  IconFileText,
  IconFolders,
  IconHistory,
  IconRefresh,
  IconSearch,
  IconServer,
  IconSettings,
  IconTerminal2,
  IconX,
  IconVolume,
  IconVolumeOff,
} from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { api } from "./api/client";
import type { Environment } from "./api/types";
import { EnvBadge } from "./components/common";
import { AlertsDrawer } from "./components/AlertsDrawer";
import { useConfig, useFleet, useFleetEvents } from "./hooks/useFleet";
import { unlock } from "./lib/sounds";
import AuditPage from "./pages/AuditPage";
import FilesPage from "./pages/FilesPage";
import FleetPage from "./pages/FleetPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";
import TerminalPage from "./pages/TerminalPage";
import TunnelsPage from "./pages/TunnelsPage";
import { useFreshLogs } from "./lib/navigation";
import { EnvProvider, useEnv } from "./state/env";
import { LogsProvider } from "./state/logs";
import { TerminalProvider, useTerminals } from "./state/terminals";
import { TerminalDock } from "./components/TerminalDock";
import { envColor } from "./theme";

const NAV = [
  { to: "/fleet", label: "Fleet", icon: IconServer },
  { to: "/logs", label: "Logs", icon: IconFileText },
  { to: "/files", label: "Files", icon: IconFolders },
  { to: "/tunnels", label: "Tunnels", icon: IconArrowsExchange },
  { to: "/audit", label: "Audit", icon: IconHistory },
  { to: "/settings", label: "Settings", icon: IconSettings },
];

/** The Logs page keeps its query, its sources and its results between visits. Arriving from a
 *  Logs icon beside a host or a container is the exception — a new question — so that
 *  navigation remounts the page under a new key and it starts from the defaults. See
 *  lib/navigation. */
function LogsRoute() {
  const location = useLocation();
  const fresh = useFreshLogs();
  return <LogsPage key={fresh ? location.key : "kept"} fresh={fresh} />;
}

function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const { env, setEnv, project, setProject, envNames, inScope } = useEnv();
  const terminals = useTerminals();
  const config = useConfig();
  const fleet = useFleet();
  const live = useFleetEvents();
  const queryClient = useQueryClient();
  const sounds = config.data?.ui?.sounds;
  // Browsers gate audio behind a user gesture: the first click anywhere arms it for the session.
  useEffect(() => {
    const arm = () => unlock();
    window.addEventListener("pointerdown", arm, { once: true, passive: true });
    return () => window.removeEventListener("pointerdown", arm);
  }, []);
  const toggleSounds = async () => {
    unlock();
    try {
      await api.settings.setSounds({ enabled: !(sounds?.enabled ?? false) });
      void queryClient.invalidateQueries({ queryKey: ["config"] });
    } catch (error) {
      notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" });
    }
  };
  const [alertsOpen, setAlertsOpen] = useState(false);

  // The bell counts what is in view: one environment, or every environment of this project —
  // never another project's, which the sidebar counts would then contradict.
  const alerts = useMemo(() => {
    const all = fleet.data?.alerts ?? [];
    return all.filter((a) => {
      const host = fleet.data?.hosts.find((h) => h.alias === a.host);
      return host ? inScope(host.env) : true;
    });
  }, [fleet.data, inScope]);
  const critical = alerts.filter((a) => a.severity === "crit").length;
  // A paused host holds no connection on purpose, so it is not one the count is missing.
  const inView = useMemo(
    () => (fleet.data?.hosts ?? []).filter((h) => inScope(h.env) && !h.paused_by),
    [fleet.data, inScope],
  );

  const actions: SpotlightActionData[] = useMemo(
    () =>
      (fleet.data?.hosts ?? []).map((host) => ({
        id: host.alias,
        label: host.alias,
        description: [host.env, host.role, host.hostname].filter(Boolean).join(" · "),
        onClick: () => navigate(`/fleet?host=${encodeURIComponent(host.alias)}`),
        leftSection: <IconServer size={16} />,
      })),
    [fleet.data, navigate],
  );

  const currentProject = (config.data?.projects ?? []).find((p) => p.name === project) ?? null;
  const projectEnvironments = useMemo(
    () => (config.data?.environments ?? []).filter((e) => e.project === project),
    [config.data, project],
  );

  const renderEnvironmentLink = (environment: Environment) => {
    const all = fleet.data?.hosts.filter((h) => h.env === environment.name) ?? [];
    // "down" is measured over the hosts being watched; a paused one is neither up nor down.
    const hosts = all.filter((h) => !h.paused_by);
    const down = hosts.filter((h) => h.snapshot && !h.snapshot.reachable).length;
    const bastionsDown = environment.bastions.some((b) => fleet.data?.bastions[b] === false);
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
          bastionsDown ? (
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
        onClick={() => setEnv(environment.name)}
        variant="light"
      />
    );
  };

  const isTerminal = location.pathname.startsWith("/terminal/");
  if (isTerminal) {
    return (
      <Routes>
        <Route path="/terminal/:alias" element={<TerminalPage />} />
      </Routes>
    );
  }

  return (
    <AppShell navbar={{ width: 232, breakpoint: "sm" }} header={{ height: 52 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Tooltip label="Back to the fleet">
              <UnstyledButton onClick={() => navigate("/fleet")} aria-label="Back to the fleet">
                <Text fw={700} size="lg" style={{ letterSpacing: -0.3 }}>
                  sshfleet
                </Text>
              </UnstyledButton>
            </Tooltip>
            <Tooltip label={live.connected ? "live updates connected" : "reconnecting…"}>
              <span className={`live-dot ${live.connected ? "" : "off"}`} />
            </Tooltip>
            {live.progress && (
              <Group gap={6}>
                <Loader size={12} color="teal" />
                <Text size="xs" c="dimmed">
                  {live.progress.total ? `refreshing ${live.progress.done}/${live.progress.total}` : "refreshing…"}
                </Text>
              </Group>
            )}
          </Group>
          <Group gap="xs" wrap="nowrap">
            <UnstyledButton
              onClick={() => spotlight.open()}
              px="sm"
              py={6}
              style={{
                border: "1px solid var(--mantine-color-dark-4)",
                borderRadius: 8,
                background: "var(--mantine-color-dark-7)",
                minWidth: 260,
              }}
            >
              <Group gap={8} justify="space-between">
                <Group gap={8}>
                  <IconSearch size={15} style={{ opacity: 0.6 }} />
                  <Text size="sm" c="dimmed">
                    Jump to a host…
                  </Text>
                </Group>
                <Badge variant="default" size="xs" radius="sm">
                  ⌘K
                </Badge>
              </Group>
            </UnstyledButton>
            {/* Open shells live here, where they are reachable from every page and cover nothing.
                On the fleet the host's own row says the same thing in its place. */}
            {terminals.sessions.length > 0 && (
              <Menu position="bottom-end" width={280} withinPortal>
                <Menu.Target>
                  <Indicator label={terminals.sessions.length} size={16} color="teal" offset={4}>
                    <Tooltip label={terminals.sessions.length === 1 ? "1 shell open" : `${terminals.sessions.length} shells open`}>
                      <ActionIcon variant="default" size="lg" aria-label="open shells">
                        <IconTerminal2 size={17} />
                      </ActionIcon>
                    </Tooltip>
                  </Indicator>
                </Menu.Target>
                <Menu.Dropdown>
                  <Menu.Label>Open shells</Menu.Label>
                  {terminals.sessions.map((session) => (
                    <Menu.Item
                      key={session.id}
                      onClick={() => terminals.show(session.id)}
                      leftSection={<IconTerminal2 size={14} color={terminals.ended(session.id) ? undefined : "var(--mantine-color-teal-4)"} />}
                      rightSection={
                        <ActionIcon
                          size="sm"
                          variant="subtle"
                          color="gray"
                          aria-label={`close ${session.title}`}
                          component="span"
                          onClick={(event) => {
                            event.stopPropagation();
                            terminals.close(session.id);
                          }}
                        >
                          <IconX size={13} />
                        </ActionIcon>
                      }
                    >
                      <Text size="sm" className="mono" truncate>
                        {session.title}
                      </Text>
                      <Text size="xs" c="dimmed" truncate>
                        {terminals.ended(session.id) ? "ended" : (session.subtitle ?? session.alias)}
                      </Text>
                    </Menu.Item>
                  ))}
                </Menu.Dropdown>
              </Menu>
            )}
            {/* Refreshes what is in view: the chosen environment, else every one of this project. */}
            <Tooltip
              label={`Read the status of every host in ${env ?? project ?? "the fleet"} now. Nothing on the servers is changed.`}
              multiline
              maw={300}
            >
              <ActionIcon
                variant="default"
                size="lg"
                aria-label="refresh"
                onClick={() => void api.refresh(env ? { env } : { project: project ?? undefined })}
              >
                <IconRefresh size={17} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label={sounds?.enabled ? "Sounds on — click to mute. Choose them under Settings › Checks." : "Sounds off — click to turn them on"} multiline maw={260}>
              <ActionIcon variant="default" size="lg" aria-label="sounds" onClick={() => void toggleSounds()} color={sounds?.enabled ? "teal" : undefined}>
                {sounds?.enabled ? <IconVolume size={17} /> : <IconVolumeOff size={17} />}
              </ActionIcon>
            </Tooltip>
            <Tooltip label={alerts.length ? `${alerts.length} active alerts` : "no active alerts"}>
              <Indicator disabled={alerts.length === 0} label={alerts.length} size={16} color={critical ? "red" : "yellow"} offset={3}>
                <ActionIcon variant="default" size="lg" aria-label="alerts" onClick={() => setAlertsOpen(true)}>
                  <IconBell size={17} />
                </ActionIcon>
              </Indicator>
            </Tooltip>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm">
        <AppShell.Section>
          {/* The fleet is organised project first, then the environments inside it, so the
              sidebar reads the same way: pick the project, then look at all of it or at one
              environment of it. */}
          <Text size="xs" tt="uppercase" fw={700} c="dimmed" px="xs" mb={6} style={{ letterSpacing: 0.6 }}>
            Project
          </Text>
          {(config.data?.projects ?? []).length < 2 ? (
            <Group
              gap={8}
              wrap="nowrap"
              style={{ padding: "8px 10px", borderRadius: 8, background: "var(--mantine-color-dark-6)" }}
            >
              <Box
                w={9}
                h={9}
                style={{ borderRadius: 5, flexShrink: 0, background: `var(--mantine-color-${envColor(currentProject?.color ?? "gray")}-6)` }}
              />
              <Text size="sm" fw={600} truncate>
                {currentProject?.name ?? "No project"}
              </Text>
            </Group>
          ) : (
          <Menu position="bottom-start" width={248} withinPortal>
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
                  </Group>
                  <IconChevronDown size={14} style={{ opacity: 0.6, flexShrink: 0 }} />
                </Group>
              </UnstyledButton>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Projects</Menu.Label>
              {(config.data?.projects ?? []).map((item) => (
                <Menu.Item
                  key={item.name}
                  onClick={() => setProject(item.name)}
                  leftSection={
                    <Box w={9} h={9} style={{ borderRadius: 5, background: `var(--mantine-color-${envColor(item.color)}-6)` }} />
                  }
                  rightSection={
                    item.name === project ? <IconCheck size={14} style={{ opacity: 0.7 }} /> : undefined
                  }
                >
                  <Text size="sm" truncate>
                    {item.name}
                  </Text>
                </Menu.Item>
              ))}
              <Menu.Divider />
              <Menu.Item leftSection={<IconSettings size={14} />} onClick={() => navigate("/settings?tab=projects")}>
                Manage projects
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
          )}

          {/* Choosing here only changes what is in view; it never navigates. This is the only
              environment control in the app, so Settings depends on it to filter its own tables —
              jumping to the fleet would take that away. */}
          <Text size="xs" tt="uppercase" fw={700} c="dimmed" px="xs" mt="md" mb={6} style={{ letterSpacing: 0.6 }}>
            Environments
          </Text>
          <Stack gap={2}>
            <NavLink
              label="All environments"
              active={env === null}
              onClick={() => setEnv(null)}
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
        </AppShell.Section>

        <AppShell.Section grow component={ScrollArea} mt="md">
          <Text size="xs" tt="uppercase" fw={700} c="dimmed" px="xs" mb={6} style={{ letterSpacing: 0.6 }}>
            Views
          </Text>
          <Stack gap={2}>
            {NAV.map((item) => {
              const here = location.pathname.startsWith(item.to);
              return (
                <NavLink
                  key={item.to}
                  label={item.label}
                  leftSection={<item.icon size={17} />}
                  active={here}
                  // A view is a place you go back to: Logs comes back to the search that was
                  // on it, results and all. Only a Logs icon beside a host starts a new one.
                  onClick={() => navigate(item.to)}
                  variant="light"
                />
              );
            })}
          </Stack>
        </AppShell.Section>

        <AppShell.Section>
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
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main className="app-main" style={{ display: "flex", flexDirection: "column", height: "100vh", overflow: "auto" }}>
        {/* This page came off disk; the routes answering it were imported when the server
            started. Say so, or the mismatch shows up as unrelated-looking failures. */}
        {config.data?.stale && (
          <Alert
            color="yellow"
            variant="light"
            icon={<IconAlertTriangle size={16} />}
            title="This page is newer than the server behind it"
            mb="sm"
          >
            sshfleet was rebuilt after this server started, so parts of the interface are talking to
            routes it does not have. Stop it and start it again — nothing is saved until you do.
          </Alert>
        )}
        {env && !location.pathname.startsWith("/settings") && (
          <Group mb="sm" gap="xs">
            <Text size="sm" c="dimmed">
              Showing
            </Text>
            <EnvBadge env={env} environments={config.data?.environments} />
          </Group>
        )}
        <Routes>
          <Route path="/" element={<Navigate to="/fleet" replace />} />
          <Route path="/fleet" element={<FleetPage />} />
          <Route path="/logs" element={<LogsRoute />} />
          <Route path="/files" element={<FilesPage />} />
          <Route path="/tunnels" element={<TunnelsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/fleet" replace />} />
        </Routes>
      </AppShell.Main>

      <Spotlight
        actions={actions}
        nothingFound="No host matches"
        highlightQuery
        shortcut={["mod + K", "/"]}
        searchProps={{ leftSection: <IconSearch size={18} />, placeholder: "Jump to a host by name, address or role…" }}
        limit={12}
      />
      <AlertsDrawer opened={alertsOpen} onClose={() => setAlertsOpen(false)} alerts={alerts} />
      {/* Outside the routes: a shell keeps running while you move between pages. */}
      <TerminalDock />
    </AppShell>
  );
}

export default function App() {
  return (
    <EnvProvider>
      <TerminalProvider>
        {/* Outside the routes, both of them: a shell keeps running, and so does a log search —
            it, its results and the half-composed query all survive a trip to the fleet and back. */}
        <LogsProvider>
          <Shell />
        </LogsProvider>
      </TerminalProvider>
    </EnvProvider>
  );
}
