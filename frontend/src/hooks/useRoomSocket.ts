import { useCallback, useEffect, useRef, useState } from "react";
import { tokens } from "../api/client";

export interface RoomParticipant {
  user_id: string;
  display_name: string;
  role: string;
  cursor: number;
}

export interface ChatLine {
  actor: string;
  actor_name: string;
  text: string;
  at: number;
}

export interface RunResult {
  outcome: string;
  stdout: string;
  stderr: string;
  duration_ms?: number;
}

type Status = "connecting" | "open" | "closed" | "rejected";

/**
 * The room WebSocket.
 *
 * The version number is the whole concurrency story. Every edit is sent with
 * the version it was composed against; if the server has moved on it replies
 * with `rebase` plus the operations we missed, which we apply locally and
 * carry on. No reload, no lost cursor.
 */
export function useRoomSocket(roomId: string | null) {
  const [status, setStatus] = useState<Status>("connecting");
  const [document, setDocument] = useState("");
  const [version, setVersion] = useState(0);
  const [language, setLanguage] = useState("python");
  const [participants, setParticipants] = useState<RoomParticipant[]>([]);
  const [chat, setChat] = useState<ChatLine[]>([]);
  const [runResult, setRunResult] = useState<RunResult | null>(null);

  const socket = useRef<WebSocket | null>(null);
  const versionRef = useRef(0);
  const docRef = useRef("");
  const retries = useRef(0);
  const closing = useRef(false);

  const applyOp = (text: string, op: { start: number; end: number; text: string }) =>
    text.slice(0, op.start) + op.text + text.slice(op.end);

  useEffect(() => {
    if (!roomId) return;
    closing.current = false;

    const connect = () => {
      const token = tokens.access();
      if (!token) {
        setStatus("rejected");
        return;
      }
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(
        `${scheme}://${window.location.host}/ws/rooms/${roomId}?token=${encodeURIComponent(token)}`,
      );
      socket.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        retries.current = 0;
        setStatus("open");
      };

      ws.onmessage = (event) => {
        const frame = JSON.parse(event.data as string);
        switch (frame.type) {
          case "snapshot":
            docRef.current = frame.document ?? "";
            versionRef.current = frame.version ?? 0;
            setDocument(docRef.current);
            setVersion(versionRef.current);
            setLanguage(frame.language ?? "python");
            setParticipants(frame.participants ?? []);
            break;
          case "edit":
            if (frame.op) {
              docRef.current = applyOp(docRef.current, frame.op);
              setDocument(docRef.current);
            }
            versionRef.current = frame.version ?? versionRef.current;
            setVersion(versionRef.current);
            break;
          case "rebase":
            // We were behind. Apply what we missed, then the caller can retry.
            for (const missed of frame.ops ?? []) {
              docRef.current = applyOp(docRef.current, missed.op);
            }
            versionRef.current = frame.version ?? versionRef.current;
            setDocument(docRef.current);
            setVersion(versionRef.current);
            break;
          case "presence":
            setParticipants(frame.participants ?? []);
            break;
          case "chat":
            setChat((lines) => [
              ...lines.slice(-99),
              {
                actor: frame.actor,
                actor_name: frame.actor_name ?? "someone",
                text: frame.text,
                at: Date.now(),
              },
            ]);
            break;
          case "language":
            setLanguage(frame.language ?? "python");
            break;
          case "run_result":
            setRunResult(frame.payload as RunResult);
            break;
        }
      };

      ws.onclose = (event) => {
        if (closing.current) return;
        // 4401/4404 are our own codes for auth and membership failures.
        // Retrying those would loop forever against a decision that will not
        // change.
        if (event.code === 4401 || event.code === 4404 || event.code === 4410) {
          setStatus("rejected");
          return;
        }
        setStatus("closed");
        const delay = Math.min(8000, 500 * 2 ** retries.current++);
        window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closing.current = true;
      socket.current?.close();
    };
  }, [roomId]);

  const send = useCallback((payload: Record<string, unknown>) => {
    if (socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify(payload));
    }
  }, []);

  const edit = useCallback(
    (next: string) => {
      const previous = docRef.current;
      if (next === previous) return;

      // Collapse the change into one replace by finding the common prefix and
      // suffix. Monaco gives precise deltas, but deriving it here keeps this
      // hook usable with a plain textarea too.
      let start = 0;
      while (start < previous.length && start < next.length && previous[start] === next[start]) {
        start++;
      }
      let endOld = previous.length;
      let endNew = next.length;
      while (endOld > start && endNew > start && previous[endOld - 1] === next[endNew - 1]) {
        endOld--;
        endNew--;
      }

      docRef.current = next;
      setDocument(next);
      send({
        type: "edit",
        version: versionRef.current,
        op: { start, end: endOld, text: next.slice(start, endNew) },
      });
    },
    [send],
  );

  return {
    status,
    document,
    version,
    language,
    participants,
    chat,
    runResult,
    edit,
    sendChat: (text: string) => send({ type: "chat", text }),
    setLanguage: (lang: string) => send({ type: "language", language: lang }),
    run: (stdin = "") => send({ type: "run", text: stdin }),
  };
}
