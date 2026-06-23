import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Submission, SubmissionAccepted } from "../api/types";

/**
 * Submit code and follow it until it is graded.
 *
 * Polls rather than using a WebSocket. A submission finishes in under a
 * second, so the socket would be opened and closed faster than it is useful,
 * and polling has no reconnect story to get wrong. The backend exposes a
 * websocket_url for when this becomes worth changing.
 */
export function useSubmission() {
  const [submission, setSubmission] = useState<Submission | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  const poll = useCallback((id: string, attempt = 0) => {
    if (!mounted.current) return;
    // Back off gradually: most submissions land within a second, but a queued
    // one behind a backlog should not be polled 300 times.
    const delay = attempt < 10 ? 400 : attempt < 30 ? 1000 : 2500;

    timer.current = window.setTimeout(async () => {
      try {
        const next = await api.get<Submission>(`/submissions/${id}`);
        if (!mounted.current) return;
        setSubmission(next);

        if (next.status === "completed" || next.status === "failed") {
          setPending(false);
          // Keep polling briefly: the AI review is produced by a separate
          // task and lands after the grade.
          if (!next.ai_review && attempt < 40) poll(id, attempt + 1);
          return;
        }
        poll(id, attempt + 1);
      } catch (err) {
        if (!mounted.current) return;
        setPending(false);
        setError(err instanceof Error ? err.message : "Lost track of that submission");
      }
    }, delay);
  }, []);

  const submit = useCallback(
    async (body: {
      question_id: string;
      language?: string;
      source_code?: string;
      answer?: Record<string, unknown>;
      session_id?: string;
      is_dry_run?: boolean;
    }) => {
      setError(null);
      setPending(true);
      setSubmission(null);
      try {
        const accepted = await api.post<SubmissionAccepted>("/submissions", body, {
          // A retried POST after a dropped connection returns the original
          // submission instead of running the sandbox twice.
          idempotencyKey: crypto.randomUUID(),
        });
        poll(accepted.id);
        return accepted;
      } catch (err) {
        setPending(false);
        setError(err instanceof Error ? err.message : "Could not submit");
        return null;
      }
    },
    [poll],
  );

  return { submission, pending, error, submit };
}
