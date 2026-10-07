# ADR-0010: Elo rating for adaptive question selection, not full IRT

Status: Accepted
Date: 2026-08-23

## Context

Serving everyone the same questions is poor practice for both ends of the
distribution: a beginner is demoralised by hard problems, a strong candidate
learns nothing from easy ones. The system should converge on questions the user
has roughly a 50 to 70% chance of solving, which is where learning is fastest.

This needs a numeric estimate of both user skill and question difficulty, and
both must update as evidence arrives.

## Options considered

### Option A: Static `easy` / `medium` / `hard` labels (v1)
**Pros.** No maths. Author decides.
**Cons.** Author labels are frequently wrong. Nothing self-corrects. No
per-user notion of skill at all.

### Option B: Item Response Theory (2PL/3PL)
The standardised-testing approach. Models difficulty, discrimination and
guessing per item, and estimates ability by maximum likelihood.
**Pros.** The rigorous answer. What GRE and GMAT actually use.
**Cons.** Needs hundreds of responses per item before parameters are
meaningful. Fitting is an offline batch job, not an online update. Enormously
over-engineered for a question bank that starts empty.

### Option C: Elo
Treat "user attempts question" as a match. Expected score from the rating
difference; both ratings update by the surprise.
**Pros.** Updates online, per submission, in a few lines. Works from the first
response, no cold-start requirement. Question ratings self-correct: one that
nobody solves drifts up and stops being served to beginners. Widely understood,
which matters for explaining it.
**Cons.** One parameter per item, so it cannot model discrimination or
guessing. Rating inflation and drift need attention. The K-factor is a tuned
constant.

### Option D: FSRS / spaced repetition only
**Pros.** Excellent for *revision* scheduling.
**Cons.** Answers "when should you see this again", not "what should you see
next". Complementary, not a substitute.

## Decision

**Option C for difficulty selection, Option D for revision scheduling.** They
answer different questions and the system uses both.

```
expected  = 1 / (1 + 10 ** ((question_rating - user_rating) / 400))
actual    = score / 100                      # partial credit is partial evidence
user_rating     += K_user     * (actual - expected)
question_rating += K_question * (expected - actual)
```

With `K_user = 32` early, decaying to 16 after 30 attempts, a new user's
rating should move fast, an established one should be stable. `K_question = 8`,
lower because a question accumulates far more evidence than a user does and its
rating should be correspondingly sluggish.

Ratings are kept **per topic** as well as globally (`topic_mastery`), because
being strong at dynamic programming says little about SQL.

Using `score/100` rather than a binary pass/fail is deliberate: passing 8 of 10
test cases is real evidence about ability and throwing it away wastes most of
the signal each submission carries.

The reasoning that decided it: **IRT needs data we do not have.** With hundreds
of responses per item it would be better. With an empty question bank, Elo
gives a usable signal from the first submission, and it is the kind of system
that can be explained to a user in one sentence.

## Consequences

### What this makes easy
- Online updates, no batch job.
- Self-correcting question difficulty, independent of author labelling.
- Per-topic weakness detection falls out of the same numbers.

### What this makes hard
- Cannot express that a question discriminates well between skill levels.
- MCQ guessing is not modelled, so MCQ ratings are noisier than code ratings.
- Rating drift over long periods needs periodic recalibration.

### What we will have to revisit
- Once any question has 500+ responses, fitting a 2PL model *for that question* and comparing against its Elo rating is a genuinely interesting validation. The data model already stores what that would need.

## In short

> Elo, treating "user attempts question" as a match, so both the user's rating
> and the question's rating update on every submission. I use the fractional
> score rather than pass/fail, because passing 8 of 10 test cases is real
> evidence and binarising it throws away most of the signal. IRT is the
> rigorous answer and it's what standardised tests use, but it needs hundreds
> of responses per item before the parameters mean anything, and my question
> bank started empty. Elo gives usable signal from the first submission. The
> honest limitation is Elo has one parameter per item, so it can't model how
> well a question discriminates, and it doesn't model guessing, which makes MCQ
> ratings noisier than code ones.
