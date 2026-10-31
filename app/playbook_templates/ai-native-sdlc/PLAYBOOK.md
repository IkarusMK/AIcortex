---
title: AI-native SDLC
description: Build software with an AI agent in six stages — each stage commits an artifact the next one reads, and a person accepts it before work moves on.
category: Development
source: https://claude.com/resources/articles/the-ai-native-sdlc-playbook
stages:
- id: plan
  name: Plan
  goal: Turn an idea, ticket or production alert into a short written intent before anything is designed. Ask about whatever is unclear instead of guessing.
  artifact: intent.md
  template: |
    # Intent: <title>
    Author: <who>   Status: draft

    ## Problem
    ## Proposed outcome
    ## Affected users and systems
    ## Constraints
    ## Open questions
  approval: webui
  approver: Product owner (accepts, corrects or rejects)
- id: design
  name: Design
  goal: Work out requirements and design together in one pass from the accepted intent.md; flag every concern for the person who owns that policy (security, privacy, compliance).
  artifact: spec.md
  template: |
    # Spec: <title> (from intent.md <date>)

    ## Requirements
    ## Design
    ## Concerns and their owners
    ## Out of scope
  approval: webui
  approver: Product owner; tech lead for higher-risk changes
- id: build
  name: Build
  goal: Plan before code — write plan.md from intent.md and spec.md and get it accepted, then implement with tests, following the repository's CLAUDE.md.
  artifact: plan.md
  template: |
    # Plan: <title> (from spec.md <date>)

    ## Files that change
    ## Order of work
    ## Risks
    ## Proof (how we will know it works)
  approval: webui
  approver: Engineer; architect or tech lead for higher-risk changes
- id: test
  name: Test
  goal: Let the agent check its own work — build, tests, lint, screenshots for UI — and run the eval suite for prompt or configuration changes. For a bug fix the failing test comes first.
  artifact: test-report.md
  template: |
    # Test report: <title>

    ## Build, test and lint output
    ## Eval results
    ## What is not covered
  approval: webui
  approver: Code owner; owning team for eval-gated configuration changes
- id: deploy
  name: Deploy
  goal: Review the pull request against REVIEW.md (bugs, security, compliance with spec.md and plan.md), fix what is found, merge and release through the pipeline.
  artifact: review.md
  template: |
    # Review: <title> (PR <link>)

    ## Findings by pass (bugs / security / compliance)
    ## Important for this change
    ## Minor points (at most five)
    ## Release plan and rollback
  approval: webui
  approver: Code owner; release manager for production
- id: maintain
  name: Maintain
  goal: Watch the agreed metrics after release. A breach outside the normal band or a new ticket becomes the next intent.md, and lessons go back into CLAUDE.md, skills and evals.
  artifact: maintain.md
  template: |
    # Maintain: <title>

    ## What we watch (metric, baseline, band)
    ## What happened
    ## Decision (fix now / schedule / dismiss)
    ## Lessons for CLAUDE.md, skills and evals
  approval: webui
  approver: Service owner or on-call
plays:
- id: capture-intent
  stage: plan
  title: Capture the idea as intent.md
  requires: []
- id: requirements-design
  stage: design
  title: Requirements and design in one agent-guided session
  requires: [capture-intent, skills]
- id: plan-mode
  stage: build
  title: Plan mode by default — an accepted plan before code
  requires: []
- id: claude-md
  stage: build
  title: A short CLAUDE.md with commands, conventions and recurring corrections
  requires: []
- id: skills
  stage: build
  title: Team knowledge and policies as skills
  requires: []
- id: parallel-sessions
  stage: build
  title: Parallel sessions and subagents
  requires: [claude-md]
- id: feedback-loop
  stage: test
  title: Give the agent a way to check its own work
  requires: []
- id: continuous-evals
  stage: test
  title: An eval suite in CI, on a schedule and on configuration changes
  requires: [claude-md, feedback-loop]
- id: pr-review
  stage: deploy
  title: The agent reviews pull requests against REVIEW.md
  requires: [continuous-evals, parallel-sessions]
- id: hooks-gates
  stage: deploy
  title: Hooks as approval gates (allow, ask or block)
  requires: []
- id: ci-cd
  stage: deploy
  title: The agent in the CI/CD pipeline
  requires: [pr-review, hooks-gates]
- id: close-the-loop
  stage: maintain
  title: Metric breaches write the next intent.md
  requires: [ci-cd, capture-intent]
- id: recurring-scans
  stage: maintain
  title: Scheduled security scans of the codebase
  requires: [pr-review, hooks-gates, capture-intent]
- id: on-call
  stage: maintain
  title: The agent joins incident channels
  requires: []
---

Based on Anthropic's "The AI-native SDLC playbook" (Applied AI team, 21 August 2026),
summarized in our own words. Read the original for the reasoning and examples.

**The idea.** Agents made writing code fast; planning, review and release are now the
slow parts. So every stage ends in a small written artifact that is committed, read
by the next stage and accepted by a person. The person governs; the agent does the
legwork.

**How to run it here.** Start a run per change (`playbook_start`). The agent writes
the current stage's artifact from its template and submits it (`playbook_submit`) —
with the text, and with the repo path and commit once it is committed. The person
accepts or rejects it in the AICortex WebUI; a rejection comes back with a note, the
agent reworks and submits again. `cortex_start` shows where every run of the project
stands.

**Where to start.** Plays without prerequisites can be adopted on their own:
capture-intent, plan-mode, claude-md, skills, feedback-loop and hooks-gates. Good
first steps: turn one policy that is enforced inconsistently into a skill; generate a
CLAUDE.md and trim it to a page; collect 20–50 real tasks as the first evals.

**One source of truth per artifact.** The repository, the existing ticket system (the
agent writes back), or simply linking both sides by ID and commit — the easiest start.

**Feeding back.** A mistake the agent makes twice becomes a line in CLAUDE.md. Every
production incident and every shipped security fix gets an eval. Policies that must
always hold need a hook, because skills are advice.
