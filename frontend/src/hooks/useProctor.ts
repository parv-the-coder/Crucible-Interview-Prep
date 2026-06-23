import { useEffect, useRef } from "react";
import { api } from "../api/client";

type Kind = "tab_blur" | "fullscreen_exit" | "paste_large" | "copy" | "devtools";

/**
 * Report proctoring events during a timed test.
 *
 * These are browser-reported and therefore defeatable by anyone who opens the
 * console. That is a real limitation, not something to paper over: this raises
 * the cost of casual cheating and does not stop a motivated attempt. Genuine
 * proctoring needs a lockdown browser or video, both out of scope.
 *
 * The server decides what a violation means. This only reports.
 */
export function useProctor(
  sessionId: string | null,
  active: boolean,
  onAction?: (action: string, count: number) => void,
) {
  // Deduplicate bursts: switching windows can fire blur and visibilitychange
  // together, and reporting both would spend two of the candidate's three
  // strikes on one action.
  const lastReport = useRef(0);

  useEffect(() => {
    if (!sessionId || !active) return;

    const report = async (kind: Kind, detail: Record<string, unknown> = {}) => {
      const now = Date.now();
      if (now - lastReport.current < 1200) return;
      lastReport.current = now;
      try {
        const result = await api.post<{ action: string; running_count: number }>(
          `/sessions/${sessionId}/violations`,
          { kind, detail },
        );
        onAction?.(result.action, result.running_count);
      } catch {
        // A failed report must not interrupt the test. The server-side
        // deadline still applies regardless.
      }
    };

    const onVisibility = () => {
      if (document.hidden) void report("tab_blur", { at: new Date().toISOString() });
    };
    const onBlur = () => void report("tab_blur", { via: "window-blur" });
    const onFullscreen = () => {
      if (!document.fullscreenElement) void report("fullscreen_exit");
    };
    const onPaste = (event: ClipboardEvent) => {
      const text = event.clipboardData?.getData("text") ?? "";
      // Small pastes are normal (a variable name, a test input). A wall of
      // text arriving at once is the thing worth noticing.
      if (text.length > 400) void report("paste_large", { length: text.length });
    };
    const onCopy = () => void report("copy");

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", onBlur);
    document.addEventListener("fullscreenchange", onFullscreen);
    document.addEventListener("paste", onPaste);
    document.addEventListener("copy", onCopy);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("fullscreenchange", onFullscreen);
      document.removeEventListener("paste", onPaste);
      document.removeEventListener("copy", onCopy);
    };
  }, [sessionId, active, onAction]);
}
