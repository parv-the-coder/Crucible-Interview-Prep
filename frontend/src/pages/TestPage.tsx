import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { SessionItem, SessionResult, TestSession } from "../api/types";
import { CodeEditor } from "../components/CodeEditor";
import { useAutosave } from "../hooks/useAutosave";
import { useCountdown } from "../hooks/useCountdown";
import { useProctor } from "../hooks/useProctor";

export function TestPage() {
  const [session, setSession] = useState<TestSession | null>(null);
  const [result, setResult] = useState<SessionResult | null>(null);
  const [current, setCurrent] = useState(0);
  const [warning, setWarning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState({ question_count: 3, duration_minutes: 30, adaptive: true });

  const active = session?.status === "active";

  const finish = useCallback(async () => {
    if (!session) return;
    try {
      setResult(await api.post<SessionResult>(`/sessions/${session.id}/submit`));
      setSession((s) => (s ? { ...s, status: "submitted" } : s));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit");
    }
  }, [session]);

  const { label, critical } = useCountdown(session?.seconds_remaining ?? 0, () => {
    if (active) void finish();
  });

  useProctor(session?.id ?? null, active, (action, count) => {
    if (action === "auto_submitted") {
      setWarning("Session ended: too many violations.");
      void finish();
    } else {
      setWarning(`Warning ${count} of 3: stay on this tab during the test.`);
    }
  });

  const start = async () => {
    setError(null);
    try {
      const created = await api.post<TestSession>("/sessions", config);
      setSession(created);
      setResult(null);
      setCurrent(0);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start");
    }
  };

  if (result) return <ResultView result={result} onAgain={() => { setSession(null); setResult(null); }} />;

  if (!session) {
    return (
      <div className="pad start-test">
        <h1>Timed test</h1>
        <p className="muted">
          Questions are chosen for you and the clock runs on the server. Switching tabs is
          recorded; three violations end the test.
        </p>
        <div className="config">
          <label>
            Questions
            <input
              type="number"
              min={1}
              max={10}
              value={config.question_count}
              onChange={(e) => setConfig({ ...config, question_count: Number(e.target.value) })}
            />
          </label>
          <label>
            Minutes
            <input
              type="number"
              min={5}
              max={120}
              value={config.duration_minutes}
              onChange={(e) => setConfig({ ...config, duration_minutes: Number(e.target.value) })}
            />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={config.adaptive}
              onChange={(e) => setConfig({ ...config, adaptive: e.target.checked })}
            />
            Match questions to my rating
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        <button onClick={start}>Start test</button>
      </div>
    );
  }

  const item = session.items[current];

  return (
    <div className="test-page">
      <header className="test-bar">
        <span className={critical ? "clock clock-critical" : "clock"}>{label}</span>
        <div className="question-tabs">
          {session.items.map((it, index) => (
            <button
              key={it.id}
              className={index === current ? "on" : ""}
              onClick={() => setCurrent(index)}
            >
              {index + 1}
            </button>
          ))}
        </div>
        <div className="spacer" />
        <button onClick={finish}>Submit test</button>
      </header>

      {warning && <div className="banner banner-warn">{warning}</div>}

      {item && <QuestionSlot key={item.id} sessionId={session.id} item={item} enabled={active} />}
    </div>
  );
}

function QuestionSlot({
  sessionId,
  item,
  enabled,
}: {
  sessionId: string;
  item: SessionItem;
  enabled: boolean;
}) {
  const [code, setCode] = useState(item.draft_code);
  const [language, setLanguage] = useState(item.draft_language ?? "python");

  // Autosaves on a debounce and flushes on unmount, so switching questions
  // never loses the last few seconds of typing.
  useAutosave(
    { code, language },
    async (value) => {
      if (!enabled) return;
      await api.put(`/sessions/${sessionId}/items/${item.id}/draft`, {
        language: value.language,
        code: value.code,
        answer: {},
      });
    },
  );

  const question = item.question;
  if (!question) return null;

  return (
    <div className="solve-page">
      <section className="prompt-pane">
        <h2>{question.title}</h2>
        <p className="topic-line">
          {question.topic} · {question.difficulty}
        </p>
        <div className="prompt-body">{question.prompt}</div>
        {question.sample_test_cases.map((tc) => (
          <div key={tc.id} className="sample">
            <div>
              <strong>Input</strong>
              <pre>{tc.stdin || "(none)"}</pre>
            </div>
            <div>
              <strong>Output</strong>
              <pre>{tc.expected_stdout}</pre>
            </div>
          </div>
        ))}
      </section>
      <section className="work-pane">
        <div className="editor-bar">
          <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={!enabled}>
            {(question.allowed_languages.length ? question.allowed_languages : ["python"]).map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
          <div className="spacer" />
          <span className="muted small">Saved automatically</span>
        </div>
        <div className="editor-holder">
          <CodeEditor value={code} language={language} onChange={setCode} readOnly={!enabled} />
        </div>
      </section>
    </div>
  );
}

function ResultView({ result, onAgain }: { result: SessionResult; onAgain: () => void }) {
  return (
    <div className="pad result-page">
      <h1>{result.percentage}%</h1>
      <p className="muted">
        {result.questions_attempted} of {result.questions_total} attempted
        {result.violation_count > 0 && ` · ${result.violation_count} violations`}
      </p>
      <p className="muted small">
        Scores fill in as each answer finishes grading. Refresh in a moment if some are still zero.
      </p>

      <h3>By topic</h3>
      <ul className="topic-scores">
        {Object.entries(result.per_topic).map(([topic, stats]) => (
          <li key={topic}>
            <span>{topic}</span>
            <div className="bar">
              <div className="bar-fill" style={{ width: `${stats.percentage ?? 0}%` }} />
            </div>
            <span>{stats.percentage ?? 0}%</span>
          </li>
        ))}
      </ul>

      {result.weakest_topics.length > 0 && (
        <p className="focus">Focus next on: {result.weakest_topics.join(", ")}</p>
      )}
      <button onClick={onAgain}>Take another</button>
    </div>
  );
}
