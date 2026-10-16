import { Box, ScrollArea, Text } from "@mantine/core";

/** A unified diff, coloured line by line. */
export function DiffView({ text }: { text: string }) {
  const lines = text.split("\n");
  const shown = lines.slice(0, 3000);
  return (
    <ScrollArea.Autosize mah={420} type="auto" offsetScrollbars>
      <Box component="pre" className="mono" m={0} style={{ fontSize: 12, lineHeight: 1.5 }}>
        {shown.map((line, index) => {
          const header = line.startsWith("+++") || line.startsWith("---");
          const color = header
            ? "var(--mantine-color-dimmed)"
            : line.startsWith("+")
              ? "var(--mantine-color-teal-3)"
              : line.startsWith("-")
                ? "var(--mantine-color-red-3)"
                : line.startsWith("@@")
                  ? "var(--mantine-color-cyan-4)"
                  : undefined;
          const background =
            !header && line.startsWith("+")
              ? "rgba(32, 201, 151, 0.10)"
              : !header && line.startsWith("-")
                ? "rgba(250, 82, 82, 0.10)"
                : undefined;
          return (
            <div key={index} style={{ color, background, padding: "0 8px", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
              {line || " "}
            </div>
          );
        })}
      </Box>
      {lines.length > shown.length && (
        <Text size="xs" c="dimmed" px={8}>
          … {lines.length - shown.length} more lines
        </Text>
      )}
    </ScrollArea.Autosize>
  );
}

