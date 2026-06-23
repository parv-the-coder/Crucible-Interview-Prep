import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Page, Submission } from "../api/types";

export function HistoryPage() {
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .get<Page<Submission>>("/submissions?limit=50")
      .then((page) => setSubmissions(page.items))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="muted pad">Loading…</p>;

  return (
    <div className="pad">
      <h1>Your submissions</h1>
      {submissions.length === 0 && <p className="muted">Nothing yet. Go solve something.</p>}
      <table className="history">
        <thead>
          <tr>
            <th>When</th>
            <th>Language</th>
            <th>Result</th>
            <th>Tests</th>
            <th>Time</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {submissions.map((s) => (
            <tr key={s.id}>
              <td>{new Date(s.created_at).toLocaleString()}</td>
              <td>{s.language ?? s.type}</td>
              <td className={s.passed ? "pass" : "fail"}>
                {s.status === "completed" ? (s.passed ? "Accepted" : `${s.score}%`) : s.status}
              </td>
              <td>
                {s.cases_passed}/{s.cases_total}
              </td>
              <td>{s.execution_ms} ms</td>
              <td>
                <Link to={`/solve/${s.question_id}`}>Retry</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
