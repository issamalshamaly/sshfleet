import { UnstyledButton } from "@mantine/core";
import { IconClipboard, IconKeyboard } from "@tabler/icons-react";
import type { ReactNode } from "react";

/** What a phone keyboard cannot type: the keys a shell, a pager and an editor are driven by. */
export interface TerminalKeyHandlers {
  /** Send these bytes as if they had been typed. */
  send: (sequence: string) => void;
  /** Arrow keys say different things depending on the mode the program on the far side chose. */
  arrow: (direction: "A" | "B" | "C" | "D") => void;
  ctrl: boolean;
  toggleCtrl: () => void;
  toggleKeyboard: () => void;
  paste: () => void;
}

/** The byte Ctrl turns this character into (Ctrl+C is 0x03), or null when it means nothing. */
export function ctrlByte(character: string): string | null {
  if (character === " ") return "\0";
  if (character === "?") return "\x7f";
  const code = character.toUpperCase().charCodeAt(0);
  // @ A–Z [ \ ] ^ _ map onto 0x00–0x1f.
  return code >= 64 && code <= 95 ? String.fromCharCode(code - 64) : null;
}

/**
 * A row of the keys missing from a phone's keyboard, under the terminal on a touch screen.
 *
 * Ctrl is sticky: tap it, then the letter on the phone's own keyboard. No key here takes focus
 * from the terminal (mousedown is cancelled), or the phone would put its keyboard away with
 * every tap.
 */
export function TerminalKeys({ send, arrow, ctrl, toggleCtrl, toggleKeyboard, paste }: TerminalKeyHandlers) {
  const key = (label: ReactNode, onPress: () => void, extra?: { name?: string; armed?: boolean; wide?: boolean }) => (
    <UnstyledButton
      className="terminal-key"
      data-armed={extra?.armed || undefined}
      data-wide={extra?.wide || undefined}
      tabIndex={-1}
      aria-label={extra?.name}
      aria-pressed={extra?.armed === undefined ? undefined : extra.armed}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onPress}
    >
      {label}
    </UnstyledButton>
  );
  return (
    <div className="terminal-keys" role="toolbar" aria-label="Terminal keys">
      {key(<IconKeyboard size={18} />, toggleKeyboard, { name: "Show or hide the keyboard" })}
      {key("Esc", () => send("\x1b"), { name: "Escape" })}
      {key("Tab", () => send("\t"), { name: "Tab" })}
      {key("Ctrl", toggleCtrl, { name: "Control, for the next key", armed: ctrl })}
      {key("←", () => arrow("D"), { name: "Left" })}
      {key("↑", () => arrow("A"), { name: "Up" })}
      {key("↓", () => arrow("B"), { name: "Down" })}
      {key("→", () => arrow("C"), { name: "Right" })}
      {key("Home", () => send("\x1b[H"), { name: "Home", wide: true })}
      {key("End", () => send("\x1b[F"), { name: "End", wide: true })}
      {key("PgUp", () => send("\x1b[5~"), { name: "Page up", wide: true })}
      {key("PgDn", () => send("\x1b[6~"), { name: "Page down", wide: true })}
      {key("|", () => send("|"))}
      {key("~", () => send("~"))}
      {key("/", () => send("/"))}
      {key("-", () => send("-"))}
      {key(<IconClipboard size={18} />, paste, { name: "Paste" })}
    </div>
  );
}
