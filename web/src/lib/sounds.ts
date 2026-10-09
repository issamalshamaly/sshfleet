/**
 * Alert sounds, synthesised with the Web Audio API so the app ships no audio files.
 *
 * Browsers refuse to make a sound before the page has had a user gesture, so `unlock()` is
 * wired to the first click anywhere; until then `play()` is a silent no-op. Everything is short
 * (under half a second) and quiet: this is a nudge for someone at another window, not a siren.
 */

export type SoundName = "none" | "chime" | "bell" | "pulse" | "soft" | "alarm";

export const SOUND_NAMES: { value: SoundName; label: string }[] = [
  { value: "none", label: "None" },
  { value: "chime", label: "Chime — two rising notes" },
  { value: "bell", label: "Bell — one clear note" },
  { value: "pulse", label: "Pulse — three short beeps" },
  { value: "soft", label: "Soft — a low tone" },
  { value: "alarm", label: "Alarm — insistent, for the serious ones" },
];

let context: AudioContext | null = null;
let unlocked = false;

function ensureContext(): AudioContext | null {
  if (typeof window === "undefined" || typeof AudioContext === "undefined") return null;
  if (!context) context = new AudioContext();
  return context;
}

/** Call from a user gesture. Safe to call many times. */
export function unlock(): void {
  const ctx = ensureContext();
  if (!ctx) return;
  if (ctx.state === "suspended") void ctx.resume();
  unlocked = true;
}

export function isUnlocked(): boolean {
  return unlocked && context?.state === "running";
}

function tone(ctx: AudioContext, at: number, frequency: number, duration: number, gain: number, type: OscillatorType = "sine"): void {
  const osc = ctx.createOscillator();
  const amp = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(frequency, at);
  amp.gain.setValueAtTime(0, at);
  amp.gain.linearRampToValueAtTime(gain, at + 0.01);
  amp.gain.exponentialRampToValueAtTime(0.0001, at + duration);
  osc.connect(amp).connect(ctx.destination);
  osc.start(at);
  osc.stop(at + duration + 0.02);
}

const PATTERNS: Record<Exclude<SoundName, "none">, (ctx: AudioContext, t: number) => void> = {
  chime: (ctx, t) => {
    tone(ctx, t, 660, 0.18, 0.18);
    tone(ctx, t + 0.16, 880, 0.28, 0.18);
  },
  bell: (ctx, t) => {
    tone(ctx, t, 988, 0.45, 0.16, "triangle");
    tone(ctx, t, 1976, 0.25, 0.05, "sine");
  },
  pulse: (ctx, t) => {
    for (let i = 0; i < 3; i += 1) tone(ctx, t + i * 0.14, 740, 0.08, 0.16, "square");
  },
  soft: (ctx, t) => {
    tone(ctx, t, 330, 0.4, 0.14);
  },
  alarm: (ctx, t) => {
    for (let i = 0; i < 4; i += 1) {
      tone(ctx, t + i * 0.22, 880, 0.1, 0.2, "square");
      tone(ctx, t + i * 0.22 + 0.11, 660, 0.1, 0.2, "square");
    }
  },
};

/** Play a named sound. Silent when the page has not been clicked yet or the name is "none". */
export function play(name: SoundName | string | undefined): void {
  if (!name || name === "none") return;
  const ctx = ensureContext();
  if (!ctx || !unlocked) return;
  const pattern = PATTERNS[name as Exclude<SoundName, "none">];
  if (!pattern) return;
  if (ctx.state === "suspended") void ctx.resume();
  pattern(ctx, ctx.currentTime + 0.01);
}

/** A desktop notification, only when the tab is hidden and permission was granted. */
export function notifyDesktop(title: string, body: string): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (typeof document !== "undefined" && document.visibilityState === "visible") return;
  try {
    const note = new Notification(title, { body, silent: true });
    note.onclick = () => {
      window.focus();
      note.close();
    };
  } catch {
    // Some browsers throw for notifications from a page context; nothing to do about it here.
  }
}

export async function requestDesktopPermission(): Promise<NotificationPermission | "unsupported"> {
  if (typeof Notification === "undefined") return "unsupported";
  if (Notification.permission !== "default") return Notification.permission;
  return Notification.requestPermission();
}
