// Mirrors the backend's Pydantic schemas. Hand-written rather than generated
// from OpenAPI: the surface is small enough that a generator is more machinery
// than it saves, and these carry comments a generator would drop.

export type Role = "student" | "interviewer" | "admin";
export type Difficulty = "easy" | "medium" | "hard";
export type QuestionType = "code" | "mcq" | "sql" | "system_design" | "behavioral";
export type SubmissionStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type SessionStatus = "active" | "submitted" | "auto_submitted" | "expired" | "abandoned";

export interface ApiError {
  error: { code: string; message: string; field: string | null };
  request_id: string | null;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  rating: number;
  is_active: boolean;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResponse {
  user: User;
  tokens: TokenPair;
}

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
  total: number | null;
  has_more: boolean;
}

export interface TestCase {
  id: string;
  ordinal: number;
  stdin: string;
  expected_stdout: string;
  explanation: string;
}

export interface QuestionSummary {
  id: string;
  slug: string;
  title: string;
  type: QuestionType;
  difficulty: Difficulty;
  topic: string;
  tags: string[];
  rating: number;
  attempt_count: number;
  pass_count: number;
}

export interface QuestionDetail extends QuestionSummary {
  prompt: string;
  constraints_md: string;
  time_limit_ms: number;
  memory_limit_mb: number;
  allowed_languages: string[];
  starter_code: Record<string, string>;
  // Sample cases only. The API has no shape that can carry hidden ones.
  sample_test_cases: TestCase[];
  public_payload: Record<string, unknown>;
}

export interface CaseResult {
  ordinal: number;
  outcome: string;
  execution_ms: number;
  memory_kb: number;
  stdout: string;
  stderr: string;
  is_visible: boolean;
}

export interface AiReview {
  summary?: string;
  correctness?: string;
  complexity?: { time: string; space: string };
  strengths?: string[];
  improvements?: string[];
  rubric?: Record<string, number>;
  follow_up?: { question: string; why: string };
}

export interface Submission {
  id: string;
  question_id: string;
  type: QuestionType;
  language: string | null;
  status: SubmissionStatus;
  score: number;
  max_score: number;
  passed: boolean;
  cases_passed: number;
  cases_total: number;
  is_dry_run: boolean;
  execution_ms: number;
  queue_wait_ms: number;
  created_at: string;
  source_code?: string;
  compile_output?: string;
  error_message?: string;
  results?: CaseResult[];
  ai_review?: AiReview | null;
}

export interface SubmissionAccepted {
  id: string;
  status: SubmissionStatus;
  poll_url: string;
  websocket_url: string;
  deduplicated: boolean;
}

export interface SessionItem {
  id: string;
  ordinal: number;
  question_id: string;
  draft_language: string | null;
  draft_code: string;
  draft_answer: Record<string, unknown>;
  score: number;
  max_score: number;
  final_submission_id: string | null;
  question: QuestionDetail | null;
}

export interface TestSession {
  id: string;
  status: SessionStatus;
  topics: string[];
  duration_seconds: number;
  starts_at: string;
  ends_at: string;
  submitted_at: string | null;
  violation_count: number;
  total_score: number;
  max_score: number;
  items: SessionItem[];
  // Server-computed. The client counts down from this rather than deriving it
  // from ends_at, so a wrong clock on the machine does not matter.
  seconds_remaining: number;
}

export interface SessionResult {
  session_id: string;
  status: SessionStatus;
  total_score: number;
  max_score: number;
  percentage: number;
  questions_attempted: number;
  questions_total: number;
  violation_count: number;
  per_topic: Record<string, Record<string, number>>;
  weakest_topics: string[];
}

export interface Room {
  id: string;
  join_code: string;
  title: string;
  status: "waiting" | "live" | "ended";
  language: string;
  host_id: string;
  question_id: string | null;
  document: string;
  document_version: number;
  notes: string;
  participants: { user_id: string; role: string }[];
  websocket_url: string;
}
