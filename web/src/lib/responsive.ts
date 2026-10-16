import { useMediaQuery } from "@mantine/hooks";
import { useEffect } from "react";

// The same edges Mantine's own `hiddenFrom` / `visibleFrom` use (breakpoint minus 0.1px, in em),
// so a component that switches in JS and one that switches in CSS always agree on the width.
/** Phones: a bottom tab bar, cards instead of tables, drawers and forms full screen. */
export const PHONE = "(max-width: 47.99375em)";
/** Phones and tablets held upright: no permanent sidebar, Logs shows the query or the results. */
export const COMPACT = "(max-width: 61.99375em)";
/** Below the desktop width the layout was designed for: a few columns step aside. */
export const BELOW_LG = "(max-width: 74.99375em)";
/** No mouse to hover with, so a tooltip can never be seen and its words need another way in. */
export const NO_HOVER = "(hover: none)";
/** A finger, not a pointer: bigger targets, and a key bar for the terminal. */
export const COARSE = "(pointer: coarse)";

// Read synchronously, so the first paint is already the right layout instead of the desktop one
// flashing first. The app only ever renders in a browser, so there is no server value to match.
function useQuery(query: string): boolean {
  return useMediaQuery(query, undefined, { getInitialValueInEffect: false });
}

export const useIsPhone = () => useQuery(PHONE);
export const useIsCompact = () => useQuery(COMPACT);
export const useBelowLg = () => useQuery(BELOW_LG);
export const useNoHover = () => useQuery(NO_HOVER);
export const useCoarse = () => useQuery(COARSE);

export type Tier = "phone" | "tablet" | "wide";

export function useTier(): Tier {
  const phone = useIsPhone();
  const compact = useIsCompact();
  return phone ? "phone" : compact ? "tablet" : "wide";
}

/** Publishes the part of the page the on-screen keyboard leaves visible as `--vv-height` and
 *  `--vv-top` on <html>. Safari ignores `interactive-widget=resizes-content`, so without this a
 *  full-height terminal keeps its prompt underneath the keyboard. */
export function useVisualViewportVars(): void {
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const root = document.documentElement;
    let frame = 0;
    const update = () => {
      frame = 0;
      root.style.setProperty("--vv-height", `${Math.round(viewport.height)}px`);
      root.style.setProperty("--vv-top", `${Math.round(viewport.offsetTop)}px`);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    update();
    viewport.addEventListener("resize", schedule);
    viewport.addEventListener("scroll", schedule);
    return () => {
      viewport.removeEventListener("resize", schedule);
      viewport.removeEventListener("scroll", schedule);
      if (frame) cancelAnimationFrame(frame);
      root.style.removeProperty("--vv-height");
      root.style.removeProperty("--vv-top");
    };
  }, []);
}
