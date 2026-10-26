import { Badge, Box, CloseButton, Group, Progress, Text, TextInput, type MantineColor, type TextInputProps } from "@mantine/core";
import { IconPlayerPause, IconSearch } from "@tabler/icons-react";
import { useRef, type ReactNode } from "react";

import type { Environment, Host } from "../api/types";
import { pauseReason } from "../lib/pause";
import { envColor, severityColor } from "../theme";
import { InfoTip } from "./responsive";

/** `env` is an environment's id; the badge shows its short label. */
export function EnvBadge({ env, environments, size = "sm" }: { env: string; environments?: Environment[]; size?: string }) {
  const meta = environments?.find((e) => e.id === env);
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
      <InfoTip label={pauseReason(host)} multiline maw={300}>
        <Group gap={6} wrap="nowrap">
          <IconPlayerPause size={12} color="var(--mantine-color-orange-5)" />
          <Text size="sm" c="orange.4" style={{ whiteSpace: "nowrap" }}>
            paused
          </Text>
        </Group>
      </InfoTip>
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
      <InfoTip label={snapshot.error?.message ?? "unreachable"} multiline maw={360}>
        <Group gap={6} wrap="nowrap">
          <Box w={8} h={8} style={{ borderRadius: 4, background: "var(--mantine-color-red-6)" }} />
          <Text size="sm" c="red.4" fw={600} style={{ whiteSpace: "nowrap" }}>
            {snapshot.error?.kind === "bastion_down" ? "bastion down" : "unreachable"}
          </Text>
        </Group>
      </InfoTip>
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

type SearchInputProps = Omit<TextInputProps, "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
  /** Escape empties the box too. Off where the text is costly to type again, like a regex. */
  clearOnEscape?: boolean;
};

/** A box that narrows something down: the magnifier on the left and, once anything is typed, a
 *  cross on the right that empties it. Focus stays in the box, ready for the next filter. */
export function SearchInput({
  value,
  onChange,
  clearOnEscape = true,
  leftSection = <IconSearch size={15} />,
  onKeyDown,
  size,
  ...rest
}: SearchInputProps) {
  const input = useRef<HTMLInputElement>(null);
  const clear = () => {
    onChange("");
    input.current?.focus();
  };
  return (
    <TextInput
      {...rest}
      ref={input}
      size={size}
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      onKeyDown={(event) => {
        // Only when there is something to clear: an empty box lets Escape through to whatever
        // it is inside of, a drawer or a modal, which closes as it always did.
        if (clearOnEscape && event.key === "Escape" && value) {
          event.stopPropagation();
          clear();
        }
        onKeyDown?.(event);
      }}
      leftSection={leftSection}
      rightSectionPointerEvents="all"
      rightSection={
        value ? (
          <CloseButton
            size={size === "xs" ? "xs" : "sm"}
            aria-label="Clear"
            // Keeps focus in the box, so the click does not blur it first and flicker.
            onMouseDown={(event) => event.preventDefault()}
            onClick={clear}
          />
        ) : null
      }
    />
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
