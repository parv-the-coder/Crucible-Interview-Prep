# ADR-0008: Abstract the LLM provider rather than calling one SDK directly

Status: Accepted
Date: 2026-08-23

## Context

The AI features, code review against a rubric, adaptive follow-up questions,
hints, session debriefs, embeddings for semantic search, are the product
differentiator. They are also the least stable dependency in the system:

- Model names change every few months and old ones are retired.
- Pricing changes.
- Rate limits differ by provider and by tier.
- The free tier being used for development (Gemini) is not necessarily what a
  production deployment would use.
- Every provider has a different SDK shape.

Calling `google.genai` directly from a service means every one of those changes
edits business logic.

## Options considered

### Option A: Call the provider SDK directly
**Pros.** Least code. Full access to provider-specific features.
**Cons.** Provider details leak into services. Tests need network access or
heavy SDK mocking. Switching provider is a rewrite of every call site.

### Option B: Use LangChain
**Pros.** Provider abstraction already exists. Large ecosystem.
**Cons.** A very large dependency for what is, here, "send a prompt, get
structured JSON back". Its abstractions leak in their own way, and debugging
through them is worse than debugging one HTTP call. Frequent breaking changes.

### Option C: A small internal interface, one adapter per provider
**Pros.** Services depend on `AIProvider`, not on a vendor. A `FakeProvider`
makes the entire AI layer testable offline and free. Retries, budget
enforcement, cost accounting and the audit ledger live in one place rather than
being repeated per call site.
**Cons.** Written and maintained by us. Provider-specific features need
deliberate exposure.

## Decision

**Option C.** A narrow interface with these operations:

```
complete(prompt, *, purpose, schema=None, max_tokens=None) -> AIResponse
embed(texts, *, model=None) -> list[vector]
```

Adapters: `GeminiProvider` (default, free tier), `AnthropicProvider`,
`OpenAIProvider`, `OllamaProvider` (local, offline), `FakeProvider` (tests).

Cross-cutting concerns sit in the wrapper, above the adapter:

- **Budget.** Per-user daily call cap, enforced by counting `ai_interactions` rows.
- **Cache.** Identical `prompt_hash` within a TTL returns the stored response. Regrading the same submission twice costs nothing.
- **Audit ledger.** Every call recorded with prompt, response, tokens, latency and cost.
- **Structured output.** A JSON schema is requested and the response is validated with Pydantic; a malformed response is retried once, then degrades gracefully.
- **Degradation.** If the provider is down, AI features return `null` and the *deterministic* grade still stands. AI feedback is additive, it must never be able to block a submission from being graded.

That last point is the important one. The test-case grade is ground truth. The
LLM adds explanation on top of it and is never in the scoring path.

## Consequences

### What this makes easy
- The whole test suite runs offline and free against `FakeProvider`.
- Swapping provider is a config change.
- Cost is a query, not a guess.
- Prompt changes can be evaluated against stored prompt/response pairs.

### What this makes hard
- Provider-specific features (Anthropic tool use, Gemini's long context) need explicit plumbing rather than being available by default.
- The interface must be kept narrow, or it becomes a bad reimplementation of LangChain.

## In short

> Services depend on an interface, not on a vendor SDK, for two reasons. One,
> model names and pricing change constantly and I don't want that editing
> business logic. Two, and this matters more day to day, there's a
> FakeProvider, so the whole AI layer is tested offline for free. The other
> deliberate choice is that AI output is never in the scoring path: test cases
> are ground truth, the LLM only explains the result. If the provider is down,
> feedback is null and the grade still stands. I didn't use LangChain because
> for "send a prompt, get JSON back" it's a very large dependency whose
> abstractions I'd end up debugging through.
