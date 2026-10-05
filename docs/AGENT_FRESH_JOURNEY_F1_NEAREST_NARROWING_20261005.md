# Linka — F1 Final Nearest-Normalization Narrowing

Date: 2026-10-05

This addendum records only the final deterministic narrowing requested after Fresh R1–R8 and relative-date acceptance were approved for PR #202.

## Narrowed authority

`_normalize_cross_turn_nearest_after_verified_miss(...)` may advance an inherited exact missed date only when all of the following hold:

- `continues_previous == true`
- the operation type is one of `continue_active`, `availability`, `book`, or `reschedule`
- the immediately recent server-owned verified availability context is a date-level exact zero miss on the same exact date
- and either:
  - `time.mode == nearest`, or
  - `continuation_condition == if_previous_no_availability`

`time=None + continuation_condition=always` is explicitly not sufficient authority to change the date.

No customer-text parsing or regex was added.

## N1 — same-date continuation must not advance

Structured control:

```text
recent exact date = 2026-10-05
availability_option_count = 0
operation = continue_active
continues_previous = true
continuation_condition = always
time = None
inherited date = exact 2026-10-05
```

Result: **PASS** — the date remains `exact 2026-10-05`; no forced `from_date 2026-10-06` is introduced.

## N2 — typed nearest still advances

Structured control:

```text
recent exact date = 2026-10-05
availability_option_count = 0
operation = continue_active
continues_previous = true
continuation_condition = always
time.mode = nearest
inherited date = exact 2026-10-05
```

Result: **PASS** — the normalized date becomes `from_date 2026-10-06`, preserving the already-approved R8 typed-nearest path.

## Validation

Code/test head before this documentation-only addendum:

`288111af007acb51ee9ec44d70cccdc227e049c2`

Shared CI Run 1922: **SUCCESS**

- N1: PASS
- N2: PASS
- full backend suite: PASS
- Ruff: PASS
- compileall: PASS
- single Alembic head: PASS
- clean PostgreSQL migration: PASS
- frontend lint/typecheck/build: PASS

The previously approved R8 runtime semantic evidence remains typed as `time.mode=nearest`, so this narrowing preserves its forward-availability authority while removing the broad `time=None + condition=always` branch.

PR #202 remains Draft. No merge or deployment was performed.
