import {
  Box,
  Card,
  Drawer,
  Group,
  Modal,
  Popover,
  SegmentedControl,
  Select,
  Text,
  Tooltip,
  type CardProps,
  type DrawerProps,
  type GroupProps,
  type ModalProps,
  type SegmentedControlProps,
  type TooltipProps,
} from "@mantine/core";
import { useState, type KeyboardEvent, type MouseEvent, type ReactElement, type ReactNode } from "react";

import { useIsPhone, useNoHover } from "../lib/responsive";

type InfoTipProps = Omit<TooltipProps, "children"> & { children: ReactElement };

/** A tooltip that a finger can open too. With a mouse it is the ordinary Tooltip. Without one it
 *  is a small popover that a tap on the same element opens, so what the tooltip says is never out
 *  of reach on a phone. Only for information: an action must not hide behind it. */
export function InfoTip({ children, label, disabled, maw, position, multiline, ...rest }: InfoTipProps) {
  const noHover = useNoHover();
  const [opened, setOpened] = useState(false);
  if (!noHover) {
    return (
      <Tooltip label={label} disabled={disabled} maw={maw} position={position} multiline={multiline} {...rest}>
        {children}
      </Tooltip>
    );
  }
  if (disabled || label === null || label === undefined || label === "") return children;
  const toggle = (event: MouseEvent | KeyboardEvent) => {
    // The tip often sits inside a row or a card that opens something when tapped.
    event.stopPropagation();
    setOpened((open) => !open);
  };
  return (
    <Popover opened={opened} onChange={setOpened} position={position ?? "top"} withArrow shadow="md" withinPortal>
      <Popover.Target>
        <Box
          component="span"
          className="infotip-touch"
          role="button"
          tabIndex={0}
          onClick={toggle}
          onKeyDown={(event: KeyboardEvent) => {
            if (event.key === "Enter" || event.key === " ") toggle(event);
          }}
        >
          {children}
        </Box>
      </Popover.Target>
      <Popover.Dropdown maw={`min(${typeof maw === "number" ? `${maw}px` : (maw ?? "320px")}, calc(100vw - 24px))`} p="xs">
        <Text size="sm" style={{ whiteSpace: "pre-line", overflowWrap: "anywhere" }}>
          {label}
        </Text>
      </Popover.Dropdown>
    </Popover>
  );
}

/** A drawer that takes the whole screen on a phone, where a side panel would leave a sliver of
 *  page that cannot be used anyway. */
export function ResponsiveDrawer({ size, className, ...rest }: DrawerProps) {
  const phone = useIsPhone();
  return <Drawer {...rest} size={phone ? "100%" : size} className={[className, "responsive-overlay"].filter(Boolean).join(" ")} />;
}

/** A panel that slides up from the bottom and is only as tall as what it holds, for a short list
 *  of choices on a phone. */
export function BottomSheet({ className, ...rest }: Omit<DrawerProps, "position" | "size" | "styles">) {
  return (
    <Drawer
      {...rest}
      position="bottom"
      radius="lg"
      className={[className, "bottom-sheet", "responsive-overlay"].filter(Boolean).join(" ")}
      styles={{
        content: { height: "auto", maxHeight: "calc(100dvh - 48px)", borderBottomLeftRadius: 0, borderBottomRightRadius: 0 },
      }}
    />
  );
}

/** A modal that is a full page on a phone. A short confirmation (`confirm`) stays a centred box:
 *  it reads better than a page with one sentence on it. */
export function ResponsiveModal({ confirm, fullScreen, className, ...rest }: ModalProps & { confirm?: boolean }) {
  const phone = useIsPhone();
  return (
    <Modal
      {...rest}
      fullScreen={fullScreen ?? (phone && !confirm)}
      className={[className, "responsive-overlay"].filter(Boolean).join(" ")}
    />
  );
}

/** The buttons that finish a form. On a phone they stay at the bottom of the screen and share its
 *  width, so the main action is always under the thumb however long the form is. */
export function StickyActions({ children, className, ...rest }: GroupProps & { children: ReactNode }) {
  return (
    <Group justify="flex-end" gap="xs" {...rest} className={[className, "sticky-actions"].filter(Boolean).join(" ")}>
      {children}
    </Group>
  );
}

type DataCardProps = CardProps & { children: ReactNode; onClick?: () => void; selected?: boolean };

/** One record as a card, for the narrow screens where a table row would not fit. */
export function DataCard({ children, onClick, selected, className, ...rest }: DataCardProps) {
  return (
    <Card
      withBorder
      padding="sm"
      radius="md"
      {...rest}
      className={[className, onClick ? "tap-card" : undefined].filter(Boolean).join(" ")}
      data-selected={selected || undefined}
      onClick={onClick}
    >
      {children}
    </Card>
  );
}

/** A small label over its value, for a card that has no column headings to explain itself. */
export function Field({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <Box miw={0}>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Box fz="sm" style={{ overflowWrap: "anywhere" }}>
        {children}
      </Box>
    </Box>
  );
}

/** A card's buttons. They share the row, and a tap on one never also opens the card it sits on;
 *  that includes taps inside a menu it opens, which React bubbles through the portal. */
export function CardActions({ children, ...rest }: GroupProps & { children: ReactNode }) {
  return (
    <Group gap="xs" grow preventGrowOverflow={false} mt="xs" {...rest} onClick={(event) => event.stopPropagation()}>
      {children}
    </Group>
  );
}

type ResponsiveSegmentedProps = SegmentedControlProps & {
  /** What a phone gets instead: a dropdown, or the same control stacked so long labels fit. */
  phone?: "select" | "vertical";
  /** The dropdown's label on a phone, where the choices lose the context the row gave them. */
  selectLabel?: string;
};

/** A segmented control that stays usable on a phone, where its options do not fit side by side. */
export function ResponsiveSegmented({ phone: mode = "select", selectLabel, ...props }: ResponsiveSegmentedProps) {
  const phone = useIsPhone();
  if (!phone) return <SegmentedControl {...props} />;
  if (mode === "vertical") return <SegmentedControl {...props} orientation="vertical" fullWidth />;
  const data = props.data.map((item) =>
    typeof item === "string"
      ? { value: item, label: item }
      : { value: item.value, label: typeof item.label === "string" ? item.label : item.value, disabled: item.disabled },
  );
  return (
    <Select
      label={selectLabel}
      data={data}
      value={props.value ?? null}
      onChange={(value) => value !== null && props.onChange?.(value)}
      allowDeselect={false}
      disabled={props.disabled || props.readOnly}
      size={props.size === "xs" ? "sm" : props.size}
      w="100%"
      comboboxProps={{ withinPortal: true }}
    />
  );
}
