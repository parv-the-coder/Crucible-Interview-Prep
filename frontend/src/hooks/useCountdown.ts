import { useEffect, useRef, useState } from "react";

/**
 * Count down from a server-supplied number of seconds.
 *
 * Anchored to performance.now() rather than decrementing a counter each tick.
 * setInterval drifts, and a backgrounded tab is throttled hard by the browser,
 * so a naive counter can be minutes wrong by the end of a 30-minute test. The
 * server is the authority regardless; this only has to be honest on screen.
 */
export function useCountdown(initialSeconds: number, onExpire?: () => void) {
  const [remaining, setRemaining] = useState(initialSeconds);
  const startedAt = useRef(performance.now());
  const initial = useRef(initialSeconds);
  const fired = useRef(false);

  useEffect(() => {
    startedAt.current = performance.now();
    initial.current = initialSeconds;
    fired.current = false;
    setRemaining(initialSeconds);
  }, [initialSeconds]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const elapsed = (performance.now() - startedAt.current) / 1000;
      const left = Math.max(0, Math.round(initial.current - elapsed));
      setRemaining(left);
      if (left === 0 && !fired.current) {
        fired.current = true;
        onExpire?.();
      }
    }, 500);
    return () => window.clearInterval(id);
  }, [onExpire]);

  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return {
    remaining,
    label: `${minutes}:${String(seconds).padStart(2, "0")}`,
    critical: remaining <= 60,
  };
}
