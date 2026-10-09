import { Alert, Badge, Button, Code, Group, Modal, ScrollArea, Stack, Text, TextInput, Timeline } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useQuery } from "@tanstack/react-query";
import { IconAlertTriangle, IconCheck, IconRefresh, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

import { api, jobStream } from "../api/client";
import type { JobResult, JobStep } from "../api/types";

interface Props {
  alias: string;
  container: string | null;
  onClose: () => void;
}

/**
 * Restart a container in place, then watch it come back.
 *
 * Production asks for the container name to be typed. The modal stays open until the container
 * has been seen running and answering twice, or the attempt is declared failed with its last
 * log lines shown.
 */
export function RestartModal({ alias, container, onClose }: Props) {
  const opened = container !== null;
  const preview = useQuery({
    queryKey: ["restart-preview", alias, container],
    queryFn: () => api.restartPreview(alias, container!),
    enabled: opened,
  });
  const [confirm, setConfirm] = useState("");
  const [steps, setSteps] = useState<JobStep[]>([]);
  const [result, setResult] = useState<JobResult | null>(null);
  const [running, setRunning] = useState(false);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    setConfirm("");
    setSteps([]);
    setResult(null);
    setRunning(false);
    abort.current?.abort();
  }, [container]);

  const production = preview.data?.production ?? false;
  const canRun = opened && !running && !result && (!production || confirm.trim() === container);

  const start = async () => {
    if (!container) return;
    setRunning(true);
    try {
      const { job } = await api.restart(alias, container, confirm);
      abort.current = new AbortController();
      await jobStream(
        job,
        (event) => {
          if (event.type === "step") setSteps((s) => [...s, event]);
          else setResult(event);
        },
        abort.current.signal,
      );
    } catch (error) {
      notifications.show({ title: "restart failed", message: String((error as Error).message ?? error), color: "red" });
    } finally {
      setRunning(false);
    }
  };

  const phaseColor = (phase: string) => (phase === "verified" ? "teal" : phase === "failed" ? "red" : "gray");

  return (
    <Modal
      opened={opened}
      onClose={() => {
        abort.current?.abort();
        onClose();
      }}
      title={
        <Group gap="sm">
          <IconRefresh size={18} />
          <Text fw={600}>Restart container</Text>
        </Group>
      }
      size="lg"
      closeOnClickOutside={!running}
    >
      <Stack gap="md">
        {preview.data && (
          <Stack gap={6}>
            <Group gap="xs">
              <Text size="sm" c="dimmed" w={96}>
                Environment
              </Text>
              <Badge color={production ? "red" : "yellow"} variant={production ? "filled" : "light"} radius="sm" tt="uppercase">
                {preview.data.env}
              </Badge>
            </Group>
            <Group gap="xs">
              <Text size="sm" c="dimmed" w={96}>
                Host
              </Text>
              <Text size="sm" className="mono">
                {alias}
              </Text>
            </Group>
            <Group gap="xs">
              <Text size="sm" c="dimmed" w={96}>
                Container
              </Text>
              <Text size="sm" className="mono" fw={600}>
                {container}
              </Text>
              {preview.data.current && (
                <Text size="xs" c="dimmed">
                  {preview.data.current.state} · {preview.data.current.status}
                  {preview.data.current.restarts ? ` · ${preview.data.current.restarts} restarts` : ""}
                </Text>
              )}
            </Group>
            <Group gap="xs" align="flex-start">
              <Text size="sm" c="dimmed" w={96}>
                Runs
              </Text>
              <Code block style={{ flex: 1, fontSize: 12 }}>
                {preview.data.preview}
              </Code>
            </Group>
            <Text size="xs" c="dimmed">
              The container restarts in place. sshfleet never stops, removes or prunes anything.
            </Text>
          </Stack>
        )}

        {production && !result && (
          <TextInput
            label="Type the container name to confirm"
            placeholder={container ?? ""}
            value={confirm}
            onChange={(e) => setConfirm(e.currentTarget.value)}
            disabled={running}
            data-autofocus
            error={confirm && confirm.trim() !== container ? "does not match" : undefined}
          />
        )}

        {(steps.length > 0 || result) && (
          <Timeline active={steps.length} bulletSize={18} lineWidth={2}>
            {steps.map((step, index) => (
              <Timeline.Item
                key={index}
                color={phaseColor(step.phase)}
                title={
                  <Text size="sm">
                    <Text span c="dimmed" className="mono" mr={8}>
                      {step.elapsed.toFixed(0)}s
                    </Text>
                    {step.message}
                  </Text>
                }
              />
            ))}
          </Timeline>
        )}

        {result && (
          <Alert
            color={result.ok ? "teal" : "red"}
            icon={result.ok ? <IconCheck size={18} /> : <IconAlertTriangle size={18} />}
            title={result.ok ? "Restarted and verified" : "Restart did not verify"}
          >
            <Text size="sm">{result.message}</Text>
            {result.logs && (
              <ScrollArea h={180} mt="sm">
                <Code block style={{ fontSize: 11.5 }}>
                  {result.logs}
                </Code>
              </ScrollArea>
            )}
          </Alert>
        )}

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} leftSection={<IconX size={16} />}>
            {result ? "Close" : "Cancel"}
          </Button>
          {!result && (
            <Button color={production ? "red" : "teal"} onClick={start} loading={running} disabled={!canRun} leftSection={<IconRefresh size={16} />}>
              Restart
            </Button>
          )}
        </Group>
      </Stack>
    </Modal>
  );
}
