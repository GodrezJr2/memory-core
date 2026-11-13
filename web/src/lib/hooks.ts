import { animate, useMotionValue, useTransform, type MotionValue } from "framer-motion";
import { useCallback, useEffect, useState, type RefObject } from "react";
import { EASE_IOS } from "./motion";

/**
 * A number that glides to each new value (expo-out, no overshoot) and renders as text without
 * re-rendering React: pass the result as the child of a motion element.
 */
export function useAnimatedNumber(value: number, format: (n: number) => string, duration = 0.8): MotionValue<string> {
  const mv = useMotionValue(0);
  useEffect(() => {
    const controls = animate(mv, value, { duration, ease: EASE_IOS });
    return () => controls.stop();
  }, [mv, value, duration]);
  return useTransform(mv, format);
}

export function useElementWidth(ref: RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

export type Theme = "light" | "dark";

/** Theme lives on <html class="dark">; switching briefly enables colour transitions on everything. */
export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>(() =>
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );
  const setTheme = useCallback((next: Theme) => {
    const root = document.documentElement;
    root.classList.add("theme-morph");
    root.classList.toggle("dark", next === "dark");
    setThemeState(next);
    try {
      localStorage.setItem("mc-theme", next);
    } catch {
      /* private mode: the choice just isn't remembered */
    }
    window.setTimeout(() => root.classList.remove("theme-morph"), 760);
  }, []);
  return [theme, setTheme];
}
