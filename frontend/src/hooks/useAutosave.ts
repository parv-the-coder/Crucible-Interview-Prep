import { useEffect, useRef } from "react";

/**
 * Debounced autosave.
 *
 * Saving on every keystroke would be a request per character. Waiting until
 * submit risks losing work to a crashed tab. Debouncing is the middle, and the
 * flush on unmount is what stops the last few seconds of typing disappearing
 * when the candidate navigates between questions.
 */
export function useAutosave<T>(value: T, save: (value: T) => Promise<void>, delayMs = 1200) {
  const timer = useRef<number | null>(null);
  const latest = useRef(value);
  const saved = useRef<string>(JSON.stringify(value));
  const saver = useRef(save);

  useEffect(() => {
    saver.current = save;
  }, [save]);

  useEffect(() => {
    latest.current = value;
    const serialised = JSON.stringify(value);
    if (serialised === saved.current) return;

    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      saved.current = serialised;
      void saver.current(latest.current).catch(() => {
        // Let the next change retry rather than reporting a transient failure
        // to someone mid-question.
        saved.current = "";
      });
    }, delayMs);

    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [value, delayMs]);

  // Flush whatever is pending when the component goes away.
  useEffect(() => {
    return () => {
      if (JSON.stringify(latest.current) !== saved.current) {
        void saver.current(latest.current).catch(() => undefined);
      }
    };
  }, []);
}
