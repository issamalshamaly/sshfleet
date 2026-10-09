import { Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";

import { terminalSocketUrl } from "../api/client";

/** Past this, a paste is worth a second look even on one line. */
const BIG_PASTE_BYTES = 1024;

interface PendingPaste {
  text: string;
  lines: number;
  paste: (text: string) => void;
}

/** The lines a paste would enter, ignoring a single trailing newline. */
function lineCount(text: string): number {
  return (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n").length;
}

interface Props {
  alias: string;
  /** A quick action id from the server's catalogue; without one this is a plain host shell. */
  action?: string;
  container?: string;
  onExit?: () => void;
}

/**
 * A real shell on the host, inside the page. Keystrokes go to ssh over a WebSocket; ssh runs in
 * a pseudo-terminal on the tool's side and rides the connection that is already open.
 *
 * With a quick action, the same bridge runs one command on the far side instead of a login
 * shell — the command itself is built on the server, from its own catalogue.
 */
export function TerminalView({ alias, action, container: containerName, onExit }: Props) {
  const container = useRef<HTMLDivElement>(null);
  // A paste held back for confirmation. A terminal runs a line the moment its newline arrives,
  // so pasting several lines is pressing Enter several times without having read what is there
  // — which is why every desktop terminal asks first. This one used to paste it silently.
  const [pending, setPending] = useState<PendingPaste | null>(null);

  useEffect(() => {
    const element = container.current;
    if (!element) return;

    const terminal = new Terminal({
      cursorBlink: true,
      fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      scrollback: 5000,
      theme: {
        background: "#0b0f14",
        foreground: "#d6dde6",
        cursor: "#8ef0d0",
        selectionBackground: "#2d4f5f",
        black: "#0b0f14",
        brightBlack: "#5a6572",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    fit.fit();

    const socket = new WebSocket(terminalSocketUrl(alias, terminal.cols, terminal.rows, { action, container: containerName }));
    socket.binaryType = "arraybuffer";
    const encoder = new TextEncoder();
    const decoder = new TextDecoder();

    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
      terminal.focus();
    };
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) terminal.write(decoder.decode(event.data));
      else terminal.write(String(event.data));
    };
    socket.onclose = (event) => {
      const reason =
        event.code === 4401
          ? "session token rejected"
          : event.code === 4404
            ? "unknown host"
            : event.code === 4400
              ? event.reason || "this action is not available here"
              : event.code === 4502
                ? "could not connect"
                : "connection closed";
      terminal.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`);
      onExit?.();
    };

    const input = terminal.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
    });
    const resize = terminal.onResize(({ cols, rows }) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    const observer = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* element hidden */
      }
    });
    observer.observe(element);

    const onPaste = (event: ClipboardEvent) => {
      const text = event.clipboardData?.getData("text") ?? "";
      const lines = lineCount(text);
      if (lines <= 1 && text.length <= BIG_PASTE_BYTES) return; // small and single-line: just go
      event.preventDefault();
      event.stopPropagation();
      setPending({ text, lines, paste: (value) => terminal.paste(value) });
    };
    // Capture, so this runs before xterm's own handler and can still call preventDefault.
    terminal.textarea?.addEventListener("paste", onPaste, true);

    return () => {
      observer.disconnect();
      terminal.textarea?.removeEventListener("paste", onPaste, true);
      input.dispose();
      resize.dispose();
      socket.close();
      terminal.dispose();
    };
  }, [alias, action, containerName, onExit]);

  return (
    <>
      <div ref={container} className="terminal-host" />
      <Modal
        opened={pending !== null}
        onClose={() => setPending(null)}
        title={pending && pending.lines > 1 ? `Paste ${pending.lines} lines?` : "Paste this?"}
        centered
        size="md"
        // Above the terminal dock, which is fixed at 300: this asks about the terminal, so it
        // cannot open behind it. Mantine's own default would put it underneath.
        zIndex={400}
      >
        <Stack gap="sm">
          <Text size="sm">
            {pending?.lines === 1
              ? `${pending.text.length} characters into `
              : `${pending?.lines} lines into `}
            <Text span className="mono">
              {alias}
            </Text>
            . At a shell prompt each newline runs that line, so this would run{" "}
            {pending && pending.lines > 1 ? "all of them" : "it"} without you having read it
            here first. Inside an editor it is just text.
          </Text>
          <Code block style={{ maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap" }}>
            {pending?.text.slice(0, 2000)}
            {pending && pending.text.length > 2000 ? `\n… ${pending.text.length - 2000} more characters` : ""}
          </Code>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              color="teal"
              onClick={() => {
                pending?.paste(pending.text);
                setPending(null);
              }}
            >
              Paste
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
