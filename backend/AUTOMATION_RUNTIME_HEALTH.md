# Linka v0.17.2 — Automation runtime vs test artifacts

Automation health now distinguishes real runtime automation from explicit
staging/regression fixtures.

## Explicit test automation

The following are treated as test artifacts:

- rule keys starting with `staging_regression_`
- rule keys starting with `final_gate_`
- job dedupe keys starting with `staging-regression:`
- job dedupe keys starting with `final-gate-`
- known test payload markers
- workers named with the explicit Regression / Final Gate prefixes

These fixtures must not make a staging workspace look like it has a healthy
production n8n worker.

## Readiness behavior

If real runtime automation rules are enabled:

- fresh runtime worker heartbeat -> PASS
- no fresh runtime worker heartbeat -> FAIL
- real runtime stale jobs -> FAIL

If only explicit test rules/workers exist:

- `enabled_automations` -> WARN
- `automation_worker_heartbeat` -> WARN
- stale test jobs only -> WARN

This keeps Production strict without making regression residue a false blocker.

## WhatsApp transport staging

The current WhatsApp architecture keeps scheduling/provider state in Linka and
uses per-clinic Meta credentials encrypted at rest. Native Meta transport
staging must run from an isolated service/branch and test recipient; it must not
reuse or migrate a clinic production number merely to validate delivery.

## Current staging residue pattern

Jobs such as:

- `staging-regression:success_processing`
- `staging-regression:no_route_processing`
- `staging-regression:cancelled_target_processing`

are deterministic regression artifacts, not customer reminders.

## Read-only diagnosis

```powershell
python scripts/inspect_automation_runtime.py --workspace-id <WORKSPACE_UUID>
```

## Safe cleanup

Non-production only:

```powershell
python scripts/cleanup_test_automation_artifacts.py --workspace-id <WORKSPACE_UUID>
```

The cleanup targets only explicit Final Gate / staging-regression job markers.
It does not delete jobs merely because they are `processing`, `failed`, old,
or stale.

Real runtime jobs must never be bulk-deleted to make a readiness gate green.
