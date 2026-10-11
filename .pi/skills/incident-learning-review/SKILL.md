---
name: incident-learning-review
description: Convert completed human reviews and confirmed incidents into append-only, testable process-improvement proposals.
---

# Incident Learning Review

Use completed human reviews and confirmed incident records only. Never treat a
model's self-evaluation as ground truth.

1. Reconstruct the timeline from immutable event references: input, decision,
   outcome, discovery time, containment, and human disposition.
2. Separate numeric failure modes from narrative failure modes. Run one challenge
   for calculations, units, periods, and thresholds, and another for evidence,
   causality, framing, and omitted alternatives.
3. Identify the earliest control that could have detected the issue and the
   latest control that should have prevented delivery.
4. Propose a bounded rule, prompt, test, source-priority, or review-threshold
   change with sample size, expected benefit, downside, and rollback test.
5. Check the proposal against prior confirmed incidents to avoid reintroducing a
   known failure.
6. Store the proposal as a recommendation linked to evidence. Require explicit
   human change control and fresh validation before adoption.

Never rewrite history, close incidents, change configuration, or approve a
production candidate automatically.
