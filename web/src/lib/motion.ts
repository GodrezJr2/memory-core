import type { Transition } from "framer-motion";
import type { Tone } from "./types";

/** iOS-like spring (mass 1, tension 120, friction 14) for UI elements that move or resize. */
export const IOS_SPRING: Transition = { type: "spring", mass: 1, stiffness: 120, damping: 14 };

/** Expo-out curve for signals and numbers: fast off the mark, settles without overshoot. */
export const EASE_IOS = [0.16, 1, 0.3, 1] as const;

export const TONE_VAR: Record<Tone, string> = {
  recall: "var(--recall)",
  remember: "var(--remember)",
  forget: "var(--forget)",
  library: "var(--library)",
  context: "var(--context)",
  error: "var(--error)",
};

/** An agent counts as syncing for this long after its last call. */
export const LIVE_MS = 2 * 60 * 1000;
export const RECENT_MS = 60 * 60 * 1000;
