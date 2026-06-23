import type { Submission } from "../api/types";

const OUTCOME_LABEL: Record<string, string> = {
  passed: "Passed",
  wrong_answer: "Wrong answer",
  timeout: "Too slow",
  runtime_error: "Runtime error",
  compile_error: "Compile error",
  memory_exceeded: "Out of memory",
  output_truncated: "Output too large",
  internal_error: "Internal error",
};

export function ResultPanel({
  submission,
  pending,
}: {
  submission: Submission | null;
  pending: boolean;
}) {
  if (pending && !submission) {
    return <div className="result result-pending">Queued for grading…</div>;
  }
  if (!submission) {
    return <div className="result result-idle">Run your code to see results.</div>;
  }
  if (submission.status === "queued" || submission.status === "running") {
    return <div className="result result-pending">Running against test cases…</div>;
  }
  if (submission.status === "failed") {
    return (
      <div className="result result-fail">
        <strong>Could not grade this submission.</strong>
        <pre>{submission.error_message}</pre>
      </div>
    );
  }

  const compileError = submission.results?.some((r) => r.outcome === "compile_error");

  return (
    <div className={`result ${submission.passed ? "result-pass" : "result-fail"}`}>
      <header className="result-header">
        <span className="result-verdict">
          {submission.passed ? "Accepted" : "Not accepted"}
        </span>
        <span className="result-score">
          {submission.cases_passed}/{submission.cases_total} tests · {submission.score}%
        </span>
        <span className="result-timing">{submission.execution_ms} ms</span>
      </header>

      {compileError && submission.compile_output && (
        <pre className="result-compile">{submission.compile_output}</pre>
      )}

      <ol className="case-list">
        {submission.results?.map((result) => (
          <li key={result.ordinal} className={`case case-${result.outcome}`}>
            <span className="case-index">Test {result.ordinal + 1}</span>
            <span className="case-outcome">{OUTCOME_LABEL[result.outcome] ?? result.outcome}</span>
            {/* Only sample cases carry output. Hidden ones deliberately never
                leave the server, so there is nothing to render for them. */}
            {result.is_visible && result.stdout && (
              <pre className="case-output">{result.stdout}</pre>
            )}
            {result.is_visible && result.stderr && (
              <pre className="case-output case-stderr">{result.stderr}</pre>
            )}
          </li>
        ))}
      </ol>

      {submission.ai_review && <AiReviewCard review={submission.ai_review} />}
      {submission.status === "completed" && !submission.ai_review && !submission.is_dry_run && (
        <p className="ai-pending">Generating review…</p>
      )}
    </div>
  );
}

function AiReviewCard({ review }: { review: NonNullable<Submission["ai_review"]> }) {
  return (
    <section className="ai-review">
      <h3>Review</h3>
      {review.summary && <p className="ai-summary">{review.summary}</p>}

      {review.complexity && (
        <p className="ai-complexity">
          Time <code>{review.complexity.time}</code> · Space{" "}
          <code>{review.complexity.space}</code>
        </p>
      )}

      {review.rubric && (
        <ul className="ai-rubric">
          {Object.entries(review.rubric).map(([name, score]) => (
            <li key={name}>
              <span>{name}</span>
              <span className="rubric-score">{score}/5</span>
            </li>
          ))}
        </ul>
      )}

      {review.strengths?.length ? (
        <>
          <h4>What worked</h4>
          <ul>{review.strengths.map((s) => <li key={s}>{s}</li>)}</ul>
        </>
      ) : null}

      {review.improvements?.length ? (
        <>
          <h4>What to improve</h4>
          <ul>{review.improvements.map((s) => <li key={s}>{s}</li>)}</ul>
        </>
      ) : null}

      {review.follow_up && (
        <div className="ai-followup">
          <h4>Follow-up an interviewer would ask</h4>
          <p>{review.follow_up.question}</p>
          <small>{review.follow_up.why}</small>
        </div>
      )}
    </section>
  );
}
