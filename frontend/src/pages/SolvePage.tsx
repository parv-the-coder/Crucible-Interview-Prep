import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { QuestionDetail } from "../api/types";
import { CodeEditor } from "../components/CodeEditor";
import { ResultPanel } from "../components/ResultPanel";
import { useSubmission } from "../hooks/useSubmission";

export function SolvePage() {
  const { questionId } = useParams<{ questionId: string }>();
  const [question, setQuestion] = useState<QuestionDetail | null>(null);
  const [language, setLanguage] = useState("python");
  const [code, setCode] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [hint, setHint] = useState<string | null>(null);
  const [hintBusy, setHintBusy] = useState(false);
  const { submission, pending, error, submit } = useSubmission();

  useEffect(() => {
    if (!questionId) return;
    api.get<QuestionDetail>(`/questions/${questionId}`).then((q) => {
      setQuestion(q);
      const first = q.allowed_languages[0] ?? "python";
      setLanguage(first);
      setCode(q.starter_code?.[first] ?? "");
    });
  }, [questionId]);

  const changeLanguage = useCallback(
    (next: string) => {
      setLanguage(next);
      // Only replace the editor contents if the candidate has not written
      // anything yet. Clobbering their work on a dropdown change would be
      // unforgivable.
      const starter = question?.starter_code?.[next] ?? "";
      setCode((current) => {
        const previousStarter = question?.starter_code?.[language] ?? "";
        return current.trim() === "" || current === previousStarter ? starter : current;
      });
    },
    [question, language],
  );

  if (!question) return <p className="muted pad">Loading…</p>;

  const isMcq = question.type === "mcq";
  const choices = (question.public_payload?.choices ?? []) as { key: string; text: string }[];
  const multiple = Boolean(question.public_payload?.multiple);

  const run = (dry: boolean) => {
    if (!questionId) return;
    void submit({
      question_id: questionId,
      language: isMcq ? undefined : language,
      source_code: isMcq ? "" : code,
      answer: isMcq ? { selected } : {},
      is_dry_run: dry,
    });
  };

  const askForHint = async () => {
    if (!questionId) return;
    setHintBusy(true);
    try {
      const result = await api.post<{ hint: string }>("/submissions/hint", {
        question_id: questionId,
        language,
        attempt: code,
      });
      setHint(result.hint);
    } catch {
      setHint("No hint available right now.");
    } finally {
      setHintBusy(false);
    }
  };

  return (
    <div className="solve-page">
      <section className="prompt-pane">
        <header>
          <h1>{question.title}</h1>
          <span className={`difficulty difficulty-${question.difficulty}`}>
            {question.difficulty}
          </span>
        </header>
        <p className="topic-line">
          {question.topic} · {question.time_limit_ms} ms · {question.memory_limit_mb} MB
        </p>
        <div className="prompt-body">{question.prompt}</div>
        {question.constraints_md && (
          <pre className="constraints">{question.constraints_md}</pre>
        )}

        {question.sample_test_cases.length > 0 && (
          <>
            <h3>Examples</h3>
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
                {tc.explanation && <p className="sample-note">{tc.explanation}</p>}
              </div>
            ))}
          </>
        )}

        <button className="ghost" onClick={askForHint} disabled={hintBusy}>
          {hintBusy ? "Thinking…" : "Give me a hint"}
        </button>
        {hint && <p className="hint-box">{hint}</p>}
      </section>

      <section className="work-pane">
        {isMcq ? (
          <div className="mcq">
            {choices.map((choice) => (
              <label key={choice.key} className={selected.includes(choice.key) ? "on" : ""}>
                <input
                  type={multiple ? "checkbox" : "radio"}
                  name="mcq"
                  checked={selected.includes(choice.key)}
                  onChange={() =>
                    setSelected((current) =>
                      multiple
                        ? current.includes(choice.key)
                          ? current.filter((k) => k !== choice.key)
                          : [...current, choice.key]
                        : [choice.key],
                    )
                  }
                />
                <span className="choice-key">{choice.key}</span>
                {choice.text}
              </label>
            ))}
          </div>
        ) : (
          <>
            <div className="editor-bar">
              <select value={language} onChange={(e) => changeLanguage(e.target.value)}>
                {question.allowed_languages.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
              <div className="spacer" />
              <button className="ghost" onClick={() => run(true)} disabled={pending}>
                Run samples
              </button>
              <button onClick={() => run(false)} disabled={pending}>
                {pending ? "Grading…" : "Submit"}
              </button>
            </div>
            <div className="editor-holder">
              <CodeEditor value={code} language={language} onChange={setCode} />
            </div>
          </>
        )}

        {isMcq && (
          <div className="editor-bar">
            <div className="spacer" />
            <button onClick={() => run(false)} disabled={pending || selected.length === 0}>
              Submit
            </button>
          </div>
        )}

        {error && <p className="error">{error}</p>}
        <ResultPanel submission={submission} pending={pending} />
      </section>
    </div>
  );
}
