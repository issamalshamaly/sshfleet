import { Box, Button, Group, Indicator, Stack, Switch, Text, UnstyledButton } from "@mantine/core";
import { IconBell, IconChevronDown, IconDots, IconFileText, IconFolders, IconRefresh, IconServer } from "@tabler/icons-react";
import type { ComponentType } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import { useConfig, useFleetEvents } from "../../hooks/useFleet";
import { useEnv } from "../../state/env";
import { useTerminals } from "../../state/terminals";
import { useViewStore } from "../../state/views";
import { envColor } from "../../theme";
import { BottomSheet } from "../responsive";
import { EnvironmentList, NAV, NavFooter, ProjectPicker, ShellList, ViewList } from "./Navigation";

// The phone's navigation: a tab bar along the bottom for the places you move between most, and a
// button in the header for where to look. Everything the sidebar holds is reachable from one of
// the two, so nothing is lost by the sidebar not fitting.

/** What is in view, as a button: the environment (or all of them) and the project around it. */
export function WhereButton({ onClick }: { onClick: () => void }) {
  const config = useConfig();
  const { env, project } = useEnv();
  const environment = (config.data?.environments ?? []).find((e) => e.id === env);
  const projects = config.data?.projects ?? [];
  const color = environment ? envColor(environment.color) : envColor(projects.find((p) => p.name === project)?.color ?? "gray");
  return (
    <UnstyledButton
      onClick={onClick}
      aria-label="Choose the project and environment in view"
      style={{
        minWidth: 0,
        flex: "0 1 auto",
        padding: "6px 10px",
        borderRadius: 8,
        background: "var(--mantine-color-dark-6)",
        border: "1px solid var(--mantine-color-dark-4)",
      }}
    >
      <Group gap={8} wrap="nowrap">
        <Box w={8} h={8} style={{ borderRadius: 4, flexShrink: 0, background: `var(--mantine-color-${color}-6)` }} />
        <Text size="sm" fw={600} truncate tt={environment ? "capitalize" : undefined}>
          {environment ? environment.name : projects.length > 1 && project ? `${project} · all` : "All environments"}
        </Text>
        <IconChevronDown size={14} style={{ opacity: 0.6, flexShrink: 0 }} />
      </Group>
    </UnstyledButton>
  );
}

/** "Where to look": the project and the environment, which the sidebar holds on a wider screen. */
export function WhereSheet({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  return (
    <BottomSheet opened={opened} onClose={onClose} title={<Text fw={700}>Where to look</Text>}>
      <ProjectPicker onPick={onClose} />
      <EnvironmentList onPick={onClose} />
    </BottomSheet>
  );
}

/** Tunnels, Audit and Settings, the open shells, and what the header holds on a wider screen. */
export function MoreSheet({ opened, onClose, onToggleSounds }: { opened: boolean; onClose: () => void; onToggleSounds: () => void }) {
  const config = useConfig();
  const live = useFleetEvents();
  const { env, project } = useEnv();
  const sounds = config.data?.ui?.sounds;
  return (
    <BottomSheet opened={opened} onClose={onClose} title={<Text fw={700}>More</Text>}>
      <Stack gap="md">
        <Box>
          <ViewList items={NAV.slice(3)} label="Views" onPick={onClose} />
          <ShellList onPick={onClose} />
        </Box>
        <Button
          variant="default"
          leftSection={<IconRefresh size={16} />}
          onClick={() => {
            void api.refresh(env ? { env } : { project: project ?? undefined });
            onClose();
          }}
        >
          Check every host in {env ?? project ?? "the fleet"} now
        </Button>
        <Switch
          checked={!!sounds?.enabled}
          onChange={onToggleSounds}
          label="Alert sounds"
          description="Which sound plays for what is chosen under Settings › Checks."
        />
        <Text size="xs" c={live.connected ? "dimmed" : "red.4"}>
          {live.connected ? "Live updates connected" : "Reconnecting to live updates…"}
          {live.progress && ` · checking ${live.progress.total ? `${live.progress.done} of ${live.progress.total}` : "hosts"}`}
        </Text>
        <NavFooter />
      </Stack>
    </BottomSheet>
  );
}

type Tab = { key: string; label: string; icon: ComponentType<{ size?: number | string }>; active: boolean; onClick: () => void; badge?: number; badgeColor?: string; dot?: boolean };

/** The bar along the bottom of a phone: Fleet, Logs, Files, the alerts, and More. */
export function PhoneTabBar({
  alerts,
  critical,
  onAlerts,
  onMore,
  moreOpen,
}: {
  alerts: number;
  critical: number;
  onAlerts: () => void;
  onMore: () => void;
  moreOpen: boolean;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const views = useViewStore();
  const terminals = useTerminals();
  const here = (path: string) => location.pathname.startsWith(path);
  const go = (path: string) => navigate(views.resume(path));
  const tabs: Tab[] = [
    { key: "fleet", label: "Fleet", icon: IconServer, active: here("/fleet"), onClick: () => go("/fleet") },
    { key: "logs", label: "Logs", icon: IconFileText, active: here("/logs"), onClick: () => go("/logs") },
    { key: "files", label: "Files", icon: IconFolders, active: here("/files"), onClick: () => go("/files") },
    { key: "alerts", label: "Alerts", icon: IconBell, active: false, onClick: onAlerts, badge: alerts, badgeColor: critical ? "red" : "yellow" },
    {
      key: "more",
      label: "More",
      icon: IconDots,
      active: moreOpen || here("/tunnels") || here("/audit") || here("/settings"),
      onClick: onMore,
      dot: terminals.sessions.length > 0,
    },
  ];
  return (
    <Box component="nav" className="phone-tabbar" hiddenFrom="sm" aria-label="Pages">
      {tabs.map((tab) => (
        <UnstyledButton key={tab.key} className="phone-tab" data-active={tab.active || undefined} onClick={tab.onClick}>
          <Indicator
            disabled={!tab.badge && !tab.dot}
            label={tab.badge || undefined}
            size={tab.badge ? 16 : 8}
            color={tab.badgeColor ?? "teal"}
            offset={2}
          >
            <tab.icon size={22} />
          </Indicator>
          <span>{tab.label}</span>
        </UnstyledButton>
      ))}
    </Box>
  );
}
