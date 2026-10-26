import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Box,
  Burger,
  Drawer,
  Group,
  Menu,
  Indicator,
  Loader,
  Progress,
  ScrollArea,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { Spotlight, spotlight, type SpotlightActionData } from "@mantine/spotlight";
import {
  IconAlertTriangle,
  IconBell,
  IconFlask,
  IconRefresh,
  IconSearch,
  IconServer,
  IconTerminal2,
  IconX,
  IconVolume,
  IconVolumeOff,
} from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { api } from "./api/client";
import type { Alert as FleetAlert } from "./api/types";
import { EnvBadge } from "./components/common";
import { AlertsDrawer } from "./components/AlertsDrawer";
import { EnvironmentList, NavFooter, ProjectPicker, ViewList } from "./components/shell/Navigation";
import { MoreSheet, PhoneTabBar, WhereButton, WhereSheet } from "./components/shell/PhoneShell";
import { useConfig, useFleet, useFleetEvents } from "./hooks/useFleet";
import { useIsPhone, useTier, useVisualViewportVars } from "./lib/responsive";
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
import { useKeptScroll, useViewStore, ViewStateProvider } from "./state/views";
import { TerminalDock } from "./components/TerminalDock";

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
  const { env, project, inScope } = useEnv();
  const terminals = useTerminals();
  const config = useConfig();
  const fleet = useFleet();
  const live = useFleetEvents();
  const queryClient = useQueryClient();
  const views = useViewStore();
  const main = useRef<HTMLElement>(null);
  useKeptScroll(main);
  // Before the terminal route's early return: a full-page shell sizes itself from these too.
  useVisualViewportVars();
  const phone = useIsPhone();
  const tier = useTier();
  // The sidebar's stand-ins on narrower screens. They are passing panels, not places: moving to
  // another page or another width closes them, so none is left open behind a layout it no longer
  // belongs to.
  const [navOpen, setNavOpen] = useState(false);
  const [whereOpen, setWhereOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => {
    setNavOpen(false);
    setWhereOpen(false);
    setMoreOpen(false);
  }, [location.pathname, tier]);
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
  // never another project's, which the sidebar counts would then contradict. Read from each
  // host's own snapshot, the same place the fleet's Alerts card reads: the fleet-wide list is only
  // replaced when a whole sweep ends, so after a single host was checked the bell and the card
  // disagreed until the next one.
  const alerts = useMemo<FleetAlert[]>(
    () =>
      (fleet.data?.hosts ?? [])
        .filter((h) => inScope(h.env) && !h.paused_by)
        .flatMap((h) =>
          (h.snapshot?.alerts ?? []).map((a) => ({ ...a, host: h.alias, key: `${h.alias}:${a.container ?? "-"}:${a.code}` })),
        ),
    [fleet.data, inScope],
  );
  // The bell counts what the fleet's Alerts card counts — critical and warning. Notes (info) are
  // listed in the drawer, folded and counted apart, so the numbers on the page agree.
  const critical = alerts.filter((a) => a.severity === "crit").length;
  const active = alerts.filter((a) => a.severity !== "info").length;
  const notes = alerts.length - active;
  const actions: SpotlightActionData[] = useMemo(() => {
    // Every project's hosts are searched here, so each is named with its project: two projects
    // may each have a "staging".
    const where = new Map((config.data?.environments ?? []).map((e) => [e.id, `${e.project} · ${e.name}`]));
    return (fleet.data?.hosts ?? []).map((host) => ({
      id: host.alias,
      label: host.alias,
      description: [where.get(host.env) ?? host.env, host.role, host.hostname].filter(Boolean).join(" · "),
      onClick: () => navigate(`/fleet?host=${encodeURIComponent(host.alias)}`),
      leftSection: <IconServer size={16} />,
    }));
  }, [fleet.data, config.data, navigate]);

  const isTerminal = location.pathname.startsWith("/terminal/");
  if (isTerminal) {
    return (
      <Routes>
        <Route path="/terminal/:alias" element={<TerminalPage />} />
      </Routes>
    );
  }

  return (
    <AppShell
      // Below md the sidebar does not fit beside a page: a tablet opens it from the burger, a
      // phone has the tab bar and the header's environment button instead.
      navbar={{ width: 232, breakpoint: "md", collapsed: { mobile: true } }}
      header={{ height: 52 }}
      padding={{ base: 12, sm: 16 }}
    >
      <AppShell.Header className="app-header">
        <Group h="100%" px={{ base: "sm", sm: "md" }} justify="space-between" wrap="nowrap" gap="sm">
          <Group gap="sm" wrap="nowrap" style={{ flexShrink: 0 }}>
            <Burger
              opened={navOpen}
              onClick={() => setNavOpen((open) => !open)}
              size="sm"
              visibleFrom="sm"
              hiddenFrom="md"
              aria-label="Open the sidebar"
            />
            <Tooltip label="Back to the fleet">
              <UnstyledButton onClick={() => navigate(views.resume("/fleet"))} aria-label="Back to the fleet">
                <Text fw={700} size="lg" style={{ letterSpacing: -0.3 }}>
                  sshfleet
                </Text>
              </UnstyledButton>
            </Tooltip>
            <Tooltip label={live.connected ? "live updates connected" : "reconnecting…"}>
              <span className={`live-dot ${live.connected ? "" : "off"}`} />
            </Tooltip>
            {live.progress && (
              <Group gap={6} visibleFrom="sm">
                <Loader size={12} color="teal" />
                <Text size="xs" c="dimmed">
                  {live.progress.total ? `refreshing ${live.progress.done}/${live.progress.total}` : "refreshing…"}
                </Text>
              </Group>
            )}
          </Group>
          {phone ? (
            <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
              <WhereButton onClick={() => setWhereOpen(true)} />
              <ActionIcon variant="default" size="lg" aria-label="Jump to a host" onClick={() => spotlight.open()}>
                <IconSearch size={17} />
              </ActionIcon>
            </Group>
          ) : (
          <Group gap="xs" wrap="nowrap">
            <UnstyledButton
              onClick={() => spotlight.open()}
              px="sm"
              py={6}
              miw={{ base: 180, md: 260 }}
              style={{
                border: "1px solid var(--mantine-color-dark-4)",
                borderRadius: 8,
                background: "var(--mantine-color-dark-7)",
              }}
            >
              <Group gap={8} justify="space-between">
                <Group gap={8}>
                  <IconSearch size={15} style={{ opacity: 0.6 }} />
                  <Text size="sm" c="dimmed">
                    Jump to a host…
                  </Text>
                </Group>
                <Badge variant="default" size="xs" radius="sm" className="hover-only">
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
            <Tooltip
              label={`${active ? `${active} active alert${active === 1 ? "" : "s"}` : "no active alerts"}${notes ? ` · ${notes} note${notes === 1 ? "" : "s"}` : ""}`}
            >
              <Indicator disabled={active === 0} label={active} size={16} color={critical ? "red" : "yellow"} offset={3}>
                <ActionIcon variant="default" size="lg" aria-label="alerts" onClick={() => setAlertsOpen(true)}>
                  <IconBell size={17} />
                </ActionIcon>
              </Indicator>
            </Tooltip>
          </Group>
          )}
        </Group>
        {/* The phone has no room for "refreshing 3/16" in words; a hairline along the header says it. */}
        {live.progress && (
          <Progress
            hiddenFrom="sm"
            size={2}
            radius={0}
            color="teal"
            value={live.progress.total ? (100 * live.progress.done) / live.progress.total : 100}
            striped={!live.progress.total}
            animated={!live.progress.total}
            pos="absolute"
            left={0}
            right={0}
            bottom={0}
            aria-label="checking hosts"
          />
        )}
      </AppShell.Header>

      <AppShell.Navbar p="sm" visibleFrom="md">
        <AppShell.Section>
          <ProjectPicker />
          <EnvironmentList />
        </AppShell.Section>

        <AppShell.Section grow component={ScrollArea} mt="md">
          <ViewList />
        </AppShell.Section>

        <AppShell.Section>
          <NavFooter />
        </AppShell.Section>
      </AppShell.Navbar>

      {/* A tablet held upright: the same sidebar, opened from the burger over the page. */}
      <Drawer
        opened={navOpen}
        onClose={() => setNavOpen(false)}
        position="left"
        size={280}
        padding="sm"
        title={<Text fw={700}>sshfleet</Text>}
        className="responsive-overlay"
      >
        <ProjectPicker onPick={() => setNavOpen(false)} />
        <EnvironmentList onPick={() => setNavOpen(false)} />
        <Box mt="md">
          <ViewList onPick={() => setNavOpen(false)} />
        </Box>
        <Box mt="md">
          <NavFooter />
        </Box>
      </Drawer>

      <AppShell.Main ref={main} className="app-main">
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
        {config.data?.demo && (
          <Alert color="violet" variant="light" icon={<IconFlask size={16} />} mb="sm" py={6}>
            <Text size="sm">
              <b>Demo.</b> A made-up fleet: nothing here reaches a real server. Restarts, searches and
              files all answer; Ctrl+C where <code>sshfleet demo</code> runs ends it.
            </Text>
          </Alert>
        )}
        {/* A phone names the environment on the header's button already. */}
        {env && !location.pathname.startsWith("/settings") && (
          <Group mb="sm" gap="xs" visibleFrom="sm">
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
        fullScreen={phone}
      />
      <AlertsDrawer opened={alertsOpen} onClose={() => setAlertsOpen(false)} alerts={alerts} />
      {/* Outside the routes: a shell keeps running while you move between pages. */}
      <TerminalDock />
      <PhoneTabBar
        alerts={active}
        critical={critical}
        onAlerts={() => setAlertsOpen(true)}
        onMore={() => setMoreOpen(true)}
        moreOpen={moreOpen}
      />
      <WhereSheet opened={whereOpen} onClose={() => setWhereOpen(false)} />
      <MoreSheet opened={moreOpen} onClose={() => setMoreOpen(false)} onToggleSounds={() => void toggleSounds()} />
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
          {/* And every other page's filters, tabs and place, for the same reason. */}
          <ViewStateProvider>
            <Shell />
          </ViewStateProvider>
        </LogsProvider>
      </TerminalProvider>
    </EnvProvider>
  );
}
