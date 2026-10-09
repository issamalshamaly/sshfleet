import { Drawer, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { useNavigate } from "react-router-dom";

import type { Alert } from "../api/types";
import { EmptyState, SeverityBadge } from "./common";

export function AlertsDrawer({ opened, onClose, alerts }: { opened: boolean; onClose: () => void; alerts: Alert[] }) {
  const navigate = useNavigate();
  const order = { crit: 0, warn: 1, info: 2 };
  const sorted = [...alerts].sort((a, b) => order[a.severity] - order[b.severity] || a.host.localeCompare(b.host));
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="md" title={`Active alerts (${alerts.length})`}>
      {sorted.length === 0 ? (
        <EmptyState title="Nothing needs attention" hint="Alerts appear here as soon as a refresh finds one." />
      ) : (
        <Stack gap="xs">
          {sorted.map((alert) => (
            <UnstyledButton
              key={alert.key}
              onClick={() => {
                onClose();
                navigate(`/fleet?host=${encodeURIComponent(alert.host)}`);
              }}
              p="sm"
              style={{ border: "1px solid var(--mantine-color-dark-5)", borderRadius: 8 }}
            >
              <Group justify="space-between" wrap="nowrap" mb={4}>
                <SeverityBadge severity={alert.severity}>{alert.code}</SeverityBadge>
                <Text size="xs" c="dimmed" className="mono">
                  {alert.host}
                </Text>
              </Group>
              <Text size="sm">{alert.message}</Text>
            </UnstyledButton>
          ))}
        </Stack>
      )}
    </Drawer>
  );
}
