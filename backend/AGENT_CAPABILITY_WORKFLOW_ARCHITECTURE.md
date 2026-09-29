# Linka v0.14.0 — Semantic Capability Routing + Stateful Workflows

## Core principle

Linka no longer forces each customer turn into one exclusive intent.

The semantic layer describes:

- domains
- multiple simultaneous capabilities
- risk flags
- semantic entity hints
- workflow signal

It never returns implementation tool names and never grants write permission.

## Request path

Customer turn
→ auth/workspace context
→ active persisted workflow?
→ flow interpreter OR capability router
→ semantic capabilities
→ deterministic capability policy
→ filtered tool set
→ Linka customer agent
→ authorized tool
→ domain/business validation
→ PostgreSQL/integration
→ verified result
→ natural response

## Active workflows

Persisted in `conversation_flow_states`.

Initial supported flows:

- booking
- appointment_reschedule

A flow stores:

- current status
- semantic capabilities
- collected/resolved entity state
- missing information
- pending action
- exact option snapshot shown to the customer
- latest semantic decision
- version
- expiry
- lifecycle timestamps

## Why state is persisted

A message such as "خلاص الساعة 8" is interpreted inside the active booking
workflow rather than being classified from zero.

The flow interpreter returns structured semantics such as:

- continue
- modify
- select_option
- cancel_flow
- interrupt

There are no keyword tables for those decisions.

## Write authorization

The model cannot authorize a write.

Example:

`appointment_creation`
→ Python capability policy
→ allows `book_appointment`
→ active-flow state must contain a real option
→ structured selection resolves against that snapshot
→ optimistic version CAS moves the flow to `ready_to_execute`
→ only then is the write tool invoked
→ tool revalidates real availability
→ PostgreSQL prevents double booking

Risk/safety can remove write tools even when booking capabilities are present.

## Medical interruption

A turn may simultaneously contain booking semantics and medical risk.

Medical risk wins at the deterministic policy boundary:

- booking write tools are hidden
- human handoff is the only exposed execution path
- an active workflow is marked interrupted
- Team Inbox owns the conversation

## Optimistic concurrency

Each flow has a monotonically increasing `version`.

State transitions use `UPDATE ... WHERE version = expected_version`.

If two turns attempt to mutate stale state, Linka returns a conflict rather than
silently applying a write against old workflow state.

## Audit

`conversation_flow_events` records:

- start
- updates
- options presented
- write authorization
- write completion
- completion/cancellation/interruption/expiry
- conflicts

This is useful for debugging, analytics, compliance, and later onboarding flows.

## Gemini runtime

Linka now uses role-specific Gemini models while keeping the orchestration architecture model-independent:

- Semantic capability router: Gemini 3.7 Flash / low thinking
- Active flow interpreter: Gemini 3.7 Flash / low thinking
- Main customer agent: Gemini 3.7 Flash / medium thinking
- AI onboarding: Gemini 3.7 Flash / medium thinking
- Low-risk utilities: Gemini 3.5 Flash-Lite / minimal thinking

Capability routing, persisted workflows, deterministic policy, write guardrails, and database validation remain authoritative regardless of the model provider.

## Main-agent write isolation inside workflows

During an active booking/reschedule workflow, the main LLM does not receive the
corresponding write tool. It can discover and reason, but `book_appointment` /
`reschedule_appointment` are executed only by the stateful structured-selection
path after the optimistic version check.
