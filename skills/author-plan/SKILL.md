---
name: author-plan
description: Write, review, or update concise self-contained implementation plans with a linked review overview, live progress checklist, and durable knowledge preserved outside the disposable plan.
---

# Author a plan

Use this skill when writing, reviewing, or updating an implementation plan.
Follow the project's instructions and the user's approval boundaries. This
skill grants no additional permissions and does not authorize implementation,
paid evaluations, commits, or deletion of plans.

## Authoring rules

- Focus on what should happen and how completion will be verified. Omit
  unrelated alternatives, historical discussion, source inventories, and
  background that does not help review or execution.
- State behavior, boundaries, errors, and acceptance criteria clearly. Include
  implementation details only where they constrain the outcome or prevent a
  concrete mistake.
- Keep the plan self-contained. Never reference another plan or require the
  executor to read one. Incorporate necessary agreed requirements directly,
  resolving conflicts rather than copying conflicting instructions.
- Internal links to this plan's detailed sections are encouraged. References to
  actual code, tests, durable documentation, or authoritative external sources
  are allowed where needed. They must not substitute for stating the plan's
  essential requirements.
- Capture the current agreed direction, not a decision log. When direction
  changes, rewrite the affected requirements, overview, and tasks so they agree.
  Remove superseded alternatives and conflicting instructions rather than
  appending a stack of revisions. Include rationale only when it helps explain
  the final direction. Keep genuine unresolved questions clearly marked until
  resolved, then integrate the resolution into the plan.
- Distinguish agreed requirements from unresolved questions. Do not invent
  approval, validation evidence, or implementation progress.

## Required structure

### 1. Progress, immediately below the title

Include current status, a checklist, current step, blockers, and validation
evidence. Use an explicit in-progress label where checkboxes alone cannot
express status.

Write the following execution requirements into the plan itself:

- Update Progress after every meaningful step and before handoffs.
- Mark work in progress before starting and complete only with observed
  evidence. Record failed or blocked checks as they occur.
- Keep the checklist itself current as discoveries arise: add missing tasks,
  split or revise tasks, and mark obsolete tasks cancelled with a reason.
- Record concise exact check commands and observed outcomes against the current
  changes. Do not equate a worker claim or stale pass with final acceptance.
- Add discoveries that materially change scope as explicit unresolved decisions
  and follow the applicable approval rules, rather than silently expanding work.

### 2. Review overview

The user should be able to review the intended outcome from this section alone.
Start with a concise outcome statement: "After executing this plan, the outcome
will be:" followed by the concrete end state. Use a small diagram, before/after
view, directory tree, example, or other visual when it makes the outcome easier
to understand. Use fenced mermaid blocks for diagrams where appropriate.
Keep this outcome within the overview, even if a detailed Outcome section also
exists, and keep it aligned with the delivered scope.

Use concise bullets covering the important behavior, constraints, privacy or
safety guarantees, and verification. Link each bullet to the relevant detailed
section. Link to Progress for execution status.

Require the executor to keep the overview and links in sync with detailed plan
changes. Do not bury important requirements or unresolved decisions only in the
details.

### 3. Details and acceptance checks

Use only the sections the task needs. Place a horizontal rule and a
`## Implementation details` heading between the overview and the detailed plan.
Nest detailed sections beneath that heading and update internal links as needed.
This makes the boundary visible without audience labels such as "for you" or
"for the executor".

Prefer observable behavior and explicit
checks over speculative implementation prescriptions. Cover meaningful failure
modes and integration boundaries. Do not present unchecked behavior as already
verified.

### 4. Durable preservation and completion

Plans are generally disposable working material, not durable sources of truth.
Write these requirements into each plan:

- Before marking execution complete, preserve lasting behavior, rationale,
  decisions, and operational knowledge in the appropriate durable places:
  code, tests, user/developer documentation, or the project's authoritative
  change metadata. Update existing sources rather than creating duplicate
  definitions of intent.
- Do not leak plan-only details into durable execution artifacts. Shipped code,
  commands, tests, tooling, documentation, and metadata must not rely on the
  plan's path, checklist, temporary labels, or continued existence.
- Durable documents must describe the delivered result directly, not link to
  the plan as their explanation. No durable references may break if the plan
  is later removed.
- Add a checklist step and acceptance check for durable preservation. Record
  the actual durable locations in Progress when that step is complete.
- Review whether removing the plan would lose important knowledge or break the
  delivered workflow or documentation. Resolve any gaps before completion.
- Do not preserve disposable progress chatter or temporary investigation notes
  merely for completeness. Preserve what future users and maintainers need.
- Do not delete the plan automatically. Follow the user's deletion approval and
  any project retention rules.

## Final review

Before delivering an authored or updated plan:

- Confirm Progress is truthful and the overview represents all key requirements.
- Check internal section links, checklist consistency, and absence of references
  to other plans.
- Confirm durable-preservation work and its acceptance check are explicit.
- Keep plan-writing separate from feature execution. Report where the plan was
  written and any unresolved decisions, without claiming implementation passed.
