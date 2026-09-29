# Agent Core V2

This package is an isolated, non-production implementation of Linka's next conversational core.

Current phases on this branch:

1. Small model-based semantic turn contract.
2. Ephemeral model-visible catalog references with deterministic server grounding.
3. Canonical booking/reschedule state and dependency invalidation rules.
4. Pure deterministic active-task state executor for booking/reschedule progress across turns.
5. Deterministic planner and structured `TurnOutcome` contract.
6. Read-only executor over the existing clinic adapters and business read services.
7. Shared read-only package refund quoting with legacy/opening-balance safety checks.
8. Outcome builder that removes internal identifiers and prepares customer-visible verified facts.
9. One customer-facing V2 responder using native conversation roles and verified outcomes only.
10. Read-only shadow orchestrator for semantic/plan/read/outcome/reply comparison without writes or persistence.
11. Durable active-task persistence adapter over the existing conversation-flow tables, still isolated from production chat.
12. Isolated stateful V2 runtime coordinator that loads durable task state before interpretation, executes verified reads/state transitions, persists the final task once, and stops at a pending verified-write boundary instead of executing business writes.
13. Isolated real-write executor for verified booking, appointment confirmation/cancellation/reschedule, package purchase, AI follow-up scheduling, and marketing-consent updates.

Architectural invariants:

- Customer-language interpretation is owned only by the structured semantic model.
- The planner/state/read/write policy layer never receives raw customer text.
- No keyword, regex, fuzzy, or lexical fallback is allowed for intent/entity/workflow semantics.
- Python owns business policy, write authorization, state invalidation, verification, and action truth.
- Database/clinic adapters remain final execution authorities.
- The responder owns customer-facing language but cannot call tools, execute actions, or mutate state.
- Canonical database identifiers are converted to ephemeral references before semantic-model input and removed before responder input.
- The pure state executor has no SQLAlchemy/database dependency. Durable storage is a separate adapter boundary.
- Persisted V2 state is namespaced under `entity_state.agent_core_v2` with an explicit schema version; non-V2 active flows are never decoded as V2 tasks.
- V2 reuses `conversation_flow_states` / `conversation_flow_events`; this persistence step adds no new database migration.
- Database `flow.version` is the optimistic-concurrency token for persistence. V2 `task.version` remains the deterministic domain-state version used for option invalidation/authorization semantics; the two versions are intentionally separate.
- Creating V2 state must fail closed when another active V1/foreign flow owns the conversation.
- Persistence mutations do not commit the transaction. The caller remains responsible for the outer transaction boundary.
- A stateful runtime turn persists the final changed booking/reschedule task at most once. Unchanged task state is not rewritten.
- A stale persistence handle or foreign-flow conflict propagates fail-closed; the runtime does not retry by overwriting newer state.
- A stateful runtime step that reaches `write_ready` becomes a `PendingV2Write`; orchestration still does not execute it automatically.
- The isolated write executor accepts only a verified `write_ready` `PlanStep`, never raw customer text.
- Real writes reuse existing domain services and authority rules. The write executor owns one outer commit on success and rolls back on business, integrity, or authority failure; it does not retry conflicts.
- Package purchase records no invented payment: the verified package offer is created with zero initial payment and the clinic can record payment through the existing payment flow later.
- AI follow-up uses the existing CRM task/native automation scheduler path rather than a new scheduler.
- Appointment writes respect clinic integration appointment authority; external authority fails closed to staff review.
- V1 remains the production path. V2 write execution is not wired into `agent_chat.py`, the stateful orchestrator, or customer delivery yet.

Stateful runtime contract:

`orchestrator.py` remains the isolated coordinator for one durable V2 turn. It performs semantic interpretation, deterministic planning, verified reads/state transitions, and persistence. When a real business mutation is required it still returns a pending verified write. The new write executor is deliberately separate so the next cutover phase can connect action truth explicitly instead of making `write_ready` imply success.

Real-write contract:

`write_executor.py` executes one canonical verified action at a time. Appointment creation shares the existing booking rules through `appointment_creation.py`; confirmation, cancellation, and reschedule reuse `appointment_operations.py`; package purchase reuses `package_offers.py`; follow-up reuses `crm_tasks.py`; marketing consent updates the verified patient record and emits an activity event.

The executor commits only after the domain operation succeeds. Authority conflicts and policy overrides return structured handoff results. Business/integrity failures return structured non-success results after rollback. Unsupported or non-`write_ready` actions fail closed without claiming success.

Migration boundary:

The semantic, planner, read, state, responder, persistence, stateful-runtime, and isolated real-write pieces now exist. Production orchestration is still intentionally absent. The next phase may connect the V2 runtime and real-write executor behind one deliberate production entry boundary, with the existing V1 path retained until that integration is explicitly approved and validated.

Do not invoke the diagnostic shadow runtime inside V1's live database transaction merely to collect comparison data. Prefer an explicitly isolated diagnostic invocation or a separate-session/queue design before enabling automatic shadow execution.
