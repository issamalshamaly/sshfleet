import { ActionIcon, Button, Group, Modal, Paper, ScrollArea, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconMinus, IconTerminal2, IconX } from "@tabler/icons-react";
import { useCallback, useState } from "react";

import { useTerminals, type TerminalSession } from "../state/terminals";
import { TerminalView } from "./TerminalView";

/** One mounted shell. Its own component so the exit callback is stable per session: a new
 *  function identity would restart the session the moment anything else on the page re-rendered. */
function Session({ session, onEnded }: { session: TerminalSession; onEnded: (id: string) => void }) {
  const exit = useCallback(() => onEnded(session.id), [session.id, onEnded]);
  return <TerminalView alias={session.alias} action={session.action} container={session.container} onExit={exit} />;
}

/**
 * Every open shell, mounted for as long as it lives.
 *
 * A minimised session is hidden, never unmounted: unmounting closes the WebSocket and ends the
 * ssh behind it, which is exactly what "minimise" must not do. Only the active one is displayed,
 * and going from hidden to shown changes the element's size, so the terminal's own ResizeObserver
 * refits it without anything here having to know about that.
 */
export function TerminalDock() {
  const { sessions, activeId, minimise, close, closeAll, show, markEnded, ended } = useTerminals();
  const [confirming, setConfirming] = useState(false);
  if (sessions.length === 0) return null;
  const active = sessions.find((s) => s.id === activeId) ?? null;
  const running = sessions.filter((s) => !ended(s.id)).length;

  // The header X ends every session. One shell, or several that have all exited, go without
  // asking — the same as a tab's own X; several live ones are too much to lose to a stray click.
  const closeEverything = () => {
    if (running > 1) setConfirming(true);
    else closeAll();
  };

  return (
    <Paper
      withBorder
      shadow="xl"
      radius="md"
      style={{
        display: active ? "flex" : "none",
        flexDirection: "column",
        position: "fixed",
        inset: "64px 24px 24px 24px",
        zIndex: 300,
        overflow: "hidden",
        background: "var(--mantine-color-dark-8)",
      }}
    >
      <Group justify="space-between" px="sm" py={6} wrap="nowrap" style={{ borderBottom: "1px solid var(--mantine-color-dark-5)" }}>
        <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
          <IconTerminal2 size={17} style={{ flexShrink: 0 }} />
          <Text fw={600} className="mono" size="sm" truncate>
            {active?.title}
          </Text>
          {active?.subtitle && (
            <Text size="xs" c="dimmed" truncate>
              {active.subtitle}
            </Text>
          )}
        </Group>
        <Group gap={4} wrap="nowrap">
          <Tooltip label="Minimise — the shell keeps running">
            <ActionIcon variant="subtle" color="gray" aria-label="minimise terminal" onClick={minimise}>
              <IconMinus size={16} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label={sessions.length > 1 ? `Close all ${sessions.length} shells` : "Close the shell"}>
            <ActionIcon variant="subtle" color="red" aria-label="close all terminals" onClick={closeEverything}>
              <IconX size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      {/* One quick action is one session, so several are open at once soon enough. They get a
          tab strip rather than the header menu: switching between a shell and the log it is
          explaining should not mean leaving the dock. */}
      {sessions.length > 1 && (
        <ScrollArea type="never" style={{ borderBottom: "1px solid var(--mantine-color-dark-6)" }}>
          <Group gap={4} px={6} py={4} wrap="nowrap">
            {sessions.map((session) => (
              <UnstyledButton
                key={session.id}
                onClick={() => show(session.id)}
                px={10}
                py={4}
                style={{
                  borderRadius: 6,
                  flexShrink: 0,
                  background: session.id === activeId ? "var(--mantine-color-dark-5)" : "transparent",
                  border: "1px solid",
                  borderColor: session.id === activeId ? "var(--mantine-color-dark-3)" : "transparent",
                }}
              >
                <Group gap={6} wrap="nowrap">
                  <IconTerminal2 size={13} color={ended(session.id) ? "var(--mantine-color-dimmed)" : "var(--mantine-color-teal-4)"} />
                  <Text size="xs" className="mono" c={session.id === activeId ? undefined : "dimmed"}>
                    {session.subtitle ? `${session.title} · ${session.subtitle}` : session.title}
                  </Text>
                  <ActionIcon
                    size="xs"
                    variant="subtle"
                    color="gray"
                    component="span"
                    aria-label={`close ${session.title}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      close(session.id);
                    }}
                  >
                    <IconX size={11} />
                  </ActionIcon>
                </Group>
              </UnstyledButton>
            ))}
          </Group>
        </ScrollArea>
      )}

      {sessions.map((session) => (
        <div key={session.id} style={{ display: session.id === activeId ? "block" : "none", flex: 1, minHeight: 0 }}>
          <Session session={session} onEnded={markEnded} />
        </div>
      ))}

      {/* Above the dock, which sits at 300 — Mantine's default modal layer is below it. */}
      <Modal opened={confirming} onClose={() => setConfirming(false)} title="Close all shells?" centered zIndex={400}>
        <Text size="sm">
          {running} shells are still running. Closing ends every one of them, and anything running in them.
        </Text>
        <Group justify="flex-end" mt="lg">
          <Button variant="default" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
          <Button
            color="red"
            data-autofocus
            onClick={() => {
              setConfirming(false);
              closeAll();
            }}
          >
            Close all
          </Button>
        </Group>
      </Modal>
    </Paper>
  );
}
