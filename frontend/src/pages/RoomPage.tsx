import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Page, Room } from "../api/types";
import { CodeEditor } from "../components/CodeEditor";
import { useRoomSocket } from "../hooks/useRoomSocket";

export function RoomsListPage() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    api.get<Page<Room>>("/rooms").then((page) => setRooms(page.items)).catch(() => undefined);
  }, []);

  const create = async () => {
    const room = await api.post<Room>("/rooms", { title: "Interview", language: "python" });
    navigate(`/rooms/${room.id}`);
  };

  const join = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const room = await api.post<Room>("/rooms/join", { join_code: code.trim() });
      navigate(`/rooms/${room.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not join");
    }
  };

  return (
    <div className="rooms-page pad">
      <h1>Interview rooms</h1>
      <p className="muted">
        A shared editor for a live interview. One person creates a room and reads out the
        code; the other joins.
      </p>

      <div className="room-actions">
        <button onClick={create}>Create a room</button>
        <form onSubmit={join} className="join-form">
          <input
            placeholder="Join code, e.g. QVH-P2UP"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button type="submit" disabled={!code.trim()}>
            Join
          </button>
        </form>
      </div>
      {error && <p className="error">{error}</p>}

      <h3>Your rooms</h3>
      {rooms.length === 0 && <p className="muted">None yet.</p>}
      <ul className="room-list">
        {rooms.map((room) => (
          <li key={room.id}>
            <button onClick={() => navigate(`/rooms/${room.id}`)}>
              <span className="room-code">{room.join_code}</span>
              <span>{room.title}</span>
              <span className={`badge badge-${room.status}`}>{room.status}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RoomPage() {
  const { roomId } = useParams<{ roomId: string }>();
  const [room, setRoom] = useState<Room | null>(null);
  const [message, setMessage] = useState("");
  const socket = useRoomSocket(roomId ?? null);

  useEffect(() => {
    if (roomId) api.get<Room>(`/rooms/${roomId}`).then(setRoom).catch(() => undefined);
  }, [roomId]);

  if (!room) return <p className="muted pad">Loading room…</p>;

  return (
    <div className="room-page">
      <header className="room-bar">
        <span className="room-code" title="Share this code">
          {room.join_code}
        </span>
        <span className={`conn conn-${socket.status}`}>
          {socket.status === "open"
            ? "connected"
            : socket.status === "connecting"
              ? "connecting…"
              : socket.status === "rejected"
                ? "not allowed"
                : "reconnecting…"}
        </span>
        <select
          value={socket.language}
          onChange={(e) => socket.setLanguage(e.target.value)}
        >
          {["python", "javascript", "cpp"].map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <div className="spacer" />
        <span className="presence">
          {socket.participants.map((p) => (
            <span key={p.user_id} className={`pill pill-${p.role}`} title={p.role}>
              {p.display_name}
            </span>
          ))}
        </span>
        <button onClick={() => socket.run()}>Run</button>
      </header>

      <div className="room-body">
        <div className="room-editor">
          <CodeEditor
            value={socket.document}
            language={socket.language}
            onChange={socket.edit}
            readOnly={socket.status !== "open"}
          />
        </div>

        <aside className="room-side">
          <section className="room-output">
            <h3>Output</h3>
            {socket.runResult ? (
              <>
                <p className={`outcome outcome-${socket.runResult.outcome}`}>
                  {socket.runResult.outcome}
                  {socket.runResult.duration_ms != null && ` · ${socket.runResult.duration_ms} ms`}
                </p>
                <pre>{socket.runResult.stdout || socket.runResult.stderr || "(no output)"}</pre>
              </>
            ) : (
              <p className="muted">Nothing run yet.</p>
            )}
          </section>

          <section className="room-chat">
            <h3>Chat</h3>
            <ul>
              {socket.chat.map((line, index) => (
                <li key={`${line.at}-${index}`}>
                  <strong>{line.actor_name}</strong> {line.text}
                </li>
              ))}
            </ul>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (!message.trim()) return;
                socket.sendChat(message.trim());
                setMessage("");
              }}
            >
              <input
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder="Say something"
              />
            </form>
          </section>
        </aside>
      </div>
      <p className="version-note">document version {socket.version}</p>
    </div>
  );
}
