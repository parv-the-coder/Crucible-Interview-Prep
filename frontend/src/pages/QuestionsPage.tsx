import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Page, QuestionSummary } from "../api/types";

export function QuestionsPage() {
  const [questions, setQuestions] = useState<QuestionSummary[]>([]);
  const [topics, setTopics] = useState<{ topic: string; count: number }[]>([]);
  const [topic, setTopic] = useState<string>("");
  const [difficulty, setDifficulty] = useState<string>("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get<{ topic: string; count: number }[]>("/questions/topics").then(setTopics).catch(() => undefined);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    // Debounced so typing in the search box does not fire a request per key.
    const timer = window.setTimeout(async () => {
      setLoading(true);
      const params = new URLSearchParams({ limit: "50" });
      if (topic) params.set("topic", topic);
      if (difficulty) params.set("difficulty", difficulty);
      if (search.trim()) params.set("search", search.trim());
      try {
        const page = await api.get<Page<QuestionSummary>>(
          `/questions?${params}`,
          controller.signal,
        );
        setQuestions(page.items);
      } catch {
        // Aborted or failed; leave the previous list on screen.
      } finally {
        setLoading(false);
      }
    }, 250);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [topic, difficulty, search]);

  return (
    <div className="questions-page">
      <aside className="filters">
        <input
          className="search"
          placeholder="Search questions"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <h3>Difficulty</h3>
        <div className="chips">
          {["", "easy", "medium", "hard"].map((d) => (
            <button
              key={d || "any"}
              className={difficulty === d ? "chip chip-on" : "chip"}
              onClick={() => setDifficulty(d)}
            >
              {d || "Any"}
            </button>
          ))}
        </div>
        <h3>Topic</h3>
        <ul className="topic-list">
          <li>
            <button className={!topic ? "on" : ""} onClick={() => setTopic("")}>
              All topics
            </button>
          </li>
          {topics.map((t) => (
            <li key={t.topic}>
              <button className={topic === t.topic ? "on" : ""} onClick={() => setTopic(t.topic)}>
                {t.topic} <span className="count">{t.count}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="question-list">
        {loading && questions.length === 0 && <p className="muted">Loading…</p>}
        {!loading && questions.length === 0 && <p className="muted">Nothing matches those filters.</p>}
        {questions.map((q) => {
          const rate = q.attempt_count ? Math.round((q.pass_count / q.attempt_count) * 100) : null;
          return (
            <Link key={q.id} to={`/solve/${q.id}`} className="question-row">
              <span className={`difficulty difficulty-${q.difficulty}`}>{q.difficulty}</span>
              <span className="question-title">{q.title}</span>
              <span className="question-topic">{q.topic}</span>
              <span className="question-type">{q.type}</span>
              <span className="question-rate">{rate === null ? "—" : `${rate}%`}</span>
            </Link>
          );
        })}
      </section>
    </div>
  );
}
