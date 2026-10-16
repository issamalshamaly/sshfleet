import { createTheme, rem } from "@mantine/core";

// A calm dark palette for an operations tool: one accent, and the status colours (green, yellow,
// red) reserved for meaning so nothing decorative competes with them.
export const theme = createTheme({
  primaryColor: "teal",
  defaultRadius: "md",
  fontFamily:
    'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace:
    '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
  headings: { fontWeight: "600" },
  fontSizes: { xs: rem(11.5), sm: rem(13), md: rem(14), lg: rem(16), xl: rem(18) },
  components: {
    Table: { defaultProps: { verticalSpacing: "xs", horizontalSpacing: "sm" } },
    Badge: { defaultProps: { radius: "sm" } },
    // Square, so a multiple-choice control never looks like a single-choice one: the app's
    // default radius rounds a 16px box almost into a circle.
    Checkbox: { defaultProps: { radius: "xs" } },
    Tooltip: { defaultProps: { withArrow: true, openDelay: 300 } },
    // What gets typed here is aliases, paths, regexes and names typed to confirm a removal. A phone
    // keyboard that capitalises or "corrects" them turns `order-api` into `Order-API`.
    TextInput: { defaultProps: { autoCapitalize: "off", autoCorrect: "off", spellCheck: false } },
    Textarea: { defaultProps: { autoCapitalize: "off", autoCorrect: "off", spellCheck: false } },
    Autocomplete: { defaultProps: { autoCapitalize: "off", autoCorrect: "off", spellCheck: false } },
  },
});

// Meaningful colours, used everywhere the same way.
export const statusColor = {
  ok: "teal",
  warn: "yellow",
  crit: "red",
  info: "blue",
  muted: "gray",
} as const;

export function severityColor(severity: "crit" | "warn" | "info"): string {
  return severity === "crit" ? statusColor.crit : severity === "warn" ? statusColor.warn : statusColor.info;
}

export function envColor(color: string): string {
  const allowed = new Set(["red", "yellow", "green", "cyan", "blue", "grape", "orange", "teal", "pink", "violet", "gray"]);
  return allowed.has(color) ? color : "cyan";
}
