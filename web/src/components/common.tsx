import { Badge, Box, Group, Progress, Text, Tooltip, type MantineColor } from "@mantine/core";
import { IconPlayerPause } from "@tabler/icons-react";
import type { ReactNode } from "react";

import type { Environment, Host } from "../api/types";
import { envColor, severityColor } from "../theme";

export function EnvBadge({ env, environments, size = "sm" }: { env: string; environments?: Environment[]; size?: string }) {
  const meta = environments?.find((e) => e.name === env);
  return (
    <Badge color={envColor(meta?.color ?? "cyan")} variant="light" size={size} radius="sm" tt="uppercase" fw={700}>
      {meta?.short ?? env}
    </Badge>
  );
}

export function RoleBadge({ role }: { role: string }) {
  const color: Record<string, MantineColor> = {
    service: "teal",
    gateway: "grape",
    frontend: "blue",
    data: "orange",
    ops: "gray",
    unknown: "gray",
  };
  return (
    <Badge color={color[role] ?? "gray"} variant="outline" size="xs" radius="sm">
      {role}
    </Badge>
  );
}

export function SeverityBadge({ severity, children }: { severity: "crit" | "warn" | "info"; children: ReactNode }) {
  return (
    <Badge color={severityColor(severity)} variant={severity === "crit" ? "filled" : "light"} size="sm" radius="sm">
      {children}
    </Badge>
  );
}

/** Reachability as a dot plus a word, coloured by meaning. */
export function ReachStatus({ host }: { host: Host }) {
  const snapshot = host.snapshot;
  if (host.paused_by) {
    return (
      <Tooltip
        label={
          host.paused_by === host.alias
            ? "Monitoring paused — sshfleet makes no connection to this host"
            : `Reached through ${host.paused_by}, whose monitoring is paused`
        }
        multiline
        maw={300}
      >
        <Group gap={6} wrap="nowrap">
          <IconPlayerPause size={12} color="var(--mantine-color-orange-5)" />
          <Text size="sm" c="orange.4" style={{ whiteSpace: "nowrap" }}>
            paused
          </Text>
        </Group>
      </Tooltip>
    );
  }
  if (!snapshot) {
    return (
      <Group gap={6} wrap="nowrap">
        <Box w={8} h={8} style={{ borderRadius: 4, background: "var(--mantine-color-dark-3)" }} />
        <Text size="sm" c="dimmed">
          waiting
        </Text>
      </Group>
    );
  }
  if (!snapshot.reachable) {
    return (
      <Tooltip label={snapshot.error?.message ?? "unreachable"} multiline maw={360}>
        <Group gap={6} wrap="nowrap">
          <Box w={8} h={8} style={{ borderRadius: 4, background: "var(--mantine-color-red-6)" }} />
          <Text size="sm" c="red.4" fw={600} style={{ whiteSpace: "nowrap" }}>
            {snapshot.error?.kind === "bastion_down" ? "bastion down" : "unreachable"}
          </Text>
        </Group>
      </Tooltip>
    );
  }
  const stale = host.stale;
  return (
    <Group gap={6} wrap="nowrap">
      <Box
        w={8}
        h={8}
        style={{ borderRadius: 4, background: stale ? "var(--mantine-color-yellow-6)" : "var(--mantine-color-teal-5)" }}
      />
      <Text size="sm" c={stale ? "yellow.4" : "teal.4"}>
        {stale ? "stale" : "up"}
      </Text>
    </Group>
  );
}

/** A compact bar for a percentage, coloured against the configured thresholds. */
export function PercentBar({ value, warn, crit, width = 90 }: { value: number | null; warn: number; crit: number; width?: number }) {
  if (value === null || value === undefined) {
    return (
      <Text size="sm" c="dimmed">
        —
      </Text>
    );
  }
  const color = value >= crit ? "red" : value >= warn ? "yellow" : "teal";
  return (
    <Group gap={8} wrap="nowrap" w={width + 36}>
      <Progress value={Math.min(value, 100)} color={color} size="sm" w={width} radius="xl" />
      <Text size="xs" className="mono" c={value >= warn ? `${color}.4` : "dimmed"} w={30} ta="right">
        {value}%
      </Text>
    </Group>
  );
}

export function Mono({ children, ...rest }: { children: ReactNode } & React.ComponentProps<typeof Text>) {
  return (
    <Text className="mono" size="sm" {...rest}>
      {children}
    </Text>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <Box py={48} ta="center">
      <Text fw={600} c="dimmed">
        {title}
      </Text>
      {hint && (
        <Text size="sm" c="dimmed" mt={4}>
          {hint}
        </Text>
      )}
    </Box>
  );
}
