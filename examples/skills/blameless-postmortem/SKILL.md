---
name: blameless-postmortem
description: Write a blameless incident postmortem — timeline, impact, root cause, and concrete follow-ups, focused on systems not people.
category: Documentation
tags: incident, postmortem, sre, writing
---

# Blameless Postmortem

## When to use
After any incident worth learning from (outage, data issue, security event).

## Principle
Blame the **system**, not the person. People act reasonably given the information
and tools they had. If a single human error caused an outage, the system let it.

## Structure
1. **Summary** — one paragraph: what broke, who was affected, how long.
2. **Impact** — users/requests affected, duration, data integrity, money if any.
3. **Timeline** — UTC timestamps: detection → diagnosis → mitigation → resolution.
4. **Root cause** — the real underlying cause, not the trigger. Use "5 whys".
5. **What went well / what didn't** — detection speed, tooling, comms.
6. **Action items** — each with an owner and a due date; prefer systemic fixes
   (guardrails, alerts, tests) over "be more careful".

## Smell tests
- A name appears as the "cause" → reframe to the systemic gap.
- Action items are vague ("improve monitoring") → make them concrete and owned.
