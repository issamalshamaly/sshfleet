import { Box, Group, Text } from "@mantine/core";
import { IconTerminal2 } from "@tabler/icons-react";
import { useParams } from "react-router-dom";

import { TerminalView } from "../components/TerminalView";

/** Full-page terminal, for opening a host in its own browser tab next to the dashboard. */
export default function TerminalPage() {
  const { alias = "" } = useParams();
  return (
    <Box h="100vh" p="sm" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <Group gap="sm">
        <IconTerminal2 size={18} />
        <Text fw={600} className="mono">
          {alias}
        </Text>
      </Group>
      <Box style={{ flex: 1, minHeight: 0 }}>
        <TerminalView alias={alias} />
      </Box>
    </Box>
  );
}
