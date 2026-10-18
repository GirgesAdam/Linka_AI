# QA-LLM-30 — RM01–RM29 Coverage Matrix

Source baseline: 719e398927e9d4f9699cdb42c5a901cfec61e039

## Scope and status

- Specification-only change: no real model execution or database access.
- The existing 26 benchmark scenarios record evidence but have no semantic assertion evaluator; runtime exception-free is not PASS.
- Proposed deduplication has 24 execution templates and is not activated; the existing 26-scenario registry is unchanged.
- RM22 is SPEC_BLOCKED because Pulse may mean several distinct read/write operations.

## RM01–RM29 coverage

| RM ID | Capability | Existing scenario mapping | Existing coverage | Proposed template | Spec status | Main gap |
|---|---|---|---|---|---|---|
| RM01 | Service price and requested duration | simple_price, service_price_comparison | EXISTING_PROBE_PARTIAL | EX01_PRICE_AND_SIDE_READ | SPEC_READY | Existing benchmark records outcomes but does not assert price/duration equality or active-task continuity. |
| RM02 | Clinic information and payment policy | — | NEW_SCENARIO_REQUIRED | EX02_CLINIC_PAYMENT_READ | SPEC_READY | No current benchmark fixture asserts clinic knowledge/payment policy. |
| RM03 | Doctor discovery and comparison | doctor_discovery, doctor_comparison | EXISTING_PROBE_PARTIAL | EX03_DOCTOR_DISCOVERY_COMPARISON | SPEC_READY | Existing functions do not assert doctor-set completeness or earliest-slot ordering. |
| RM04 | Nearest available appointment | nearest_available | EXISTING_PROBE_PARTIAL | EX04_NEAREST_AVAILABILITY | SPEC_READY | Existing probe does not assert the first option is truly earliest. |
| RM05 | Explicit date/time availability scope | exact_time_availability, time_window_availability | EXISTING_PROBE_PARTIAL | EX05_EXPLICIT_TIME_SCOPE | SPEC_READY | Current exact-time probe uses a known available slot and does not cover an unavailable-time branch. |
| RM06 | Conditional availability fallback | conditional_fallback | EXISTING_PROBE_PARTIAL | EX06_CONDITIONAL_FALLBACK | SPEC_READY | Current scenario's verified_nonempty_anchor guarantees the primary window is not empty. |
| RM07 | Clinic-context spoken-hour resolution | — | NEW_SCENARIO_REQUIRED | EX07_SPOKEN_CLOCK_CONTEXT | SPEC_READY | No current benchmark scenario exercises real Agent interpretation for these branches. |
| RM08 | Stale option/reference rejection | change_time_during_booking | NEW_SCENARIO_REQUIRED | EX08_STALE_CHOICE_REJECTION | SPEC_READY | Current benchmark has no stale-reference assertion. |
| RM09 | Single booking and truthful confirmation | single_session_booking | EXISTING_PROBE_PARTIAL | EX09_SINGLE_BOOKING | SPEC_READY | Current main loop fails on runtime errors only and does not assert the created row. |
| RM10 | Two services in one visit | two_services_same_visit | EXISTING_PROBE_PARTIAL | EX10_GROUPED_SAME_VISIT | SPEC_READY | Current artifact omits visit_group_id from new appointment rows. |
| RM11 | Replace service mid-conversation | change_service_mid_conversation | EXISTING_PROBE_PARTIAL | EX11_SERVICE_REPLACEMENT | SPEC_READY | Existing benchmark does not assert final active task or option invalidation. |
| RM12 | Add and remove a grouped service component | two_services_same_visit | NEW_SCENARIO_REQUIRED | EX12_GROUPED_SERVICE_ADD_REMOVE | SPEC_READY | No existing benchmark scenario changes the grouped service set mid-task. |
| RM13 | Change time before a new booking | change_time_during_booking | EXISTING_PROBE_PARTIAL | EX13_TIME_CHANGE_DURING_BOOKING | SPEC_READY | Current source does not assert written start_at against the corrected time. |
| RM14 | Doctor/device/service compatibility | laser_device_consistency, two_services_same_visit | EXISTING_PROBE_PARTIAL | EX14_DEVICE_DOCTOR_COMPATIBILITY | SPEC_READY | Existing laser scenario covers only a positive compatibility path. |
| RM15 | Read upcoming appointments | cancel_existing | EXISTING_PROBE_PARTIAL | EX15_APPOINTMENT_READ_AMBIGUOUS_CANCEL | SPEC_READY | cancel_existing seeds one target and then writes; it does not prove complete-list rendering. |
| RM16 | Ambiguous cancellation target | cancel_existing | EXISTING_PROBE_PARTIAL | EX15_APPOINTMENT_READ_AMBIGUOUS_CANCEL | SPEC_READY | Need multi-target fixture and direct before/after status snapshot. |
| RM17 | Reschedule to an unavailable time | reschedule_existing | EXISTING_PROBE_PARTIAL | EX16_UNAVAILABLE_RESCHEDULE | SPEC_READY | Need fixture-derived unavailable time and direct source appointment before/after snapshot. |
| RM18 | Customer profile read | — | NEW_SCENARIO_REQUIRED | EX17_PROFILE_AND_HISTORY_READ | SPEC_READY | No current benchmark scenario reads profile. |
| RM19 | Customer visit history | — | NEW_SCENARIO_REQUIRED | EX17_PROFILE_AND_HISTORY_READ | SPEC_READY | No current benchmark scenario reads history. |
| RM20 | Session-package balance and remaining sessions | no_show_semantics | EXISTING_PROBE_PARTIAL | EX18_PACKAGE_BALANCE_REFUND_QUOTE | SPEC_READY | existing_package_booking is a booking write, not a pure balance read; no_show_semantics is closer but not asserted. |
| RM21 | Session-package offer and purchase | package_purchase, session_vs_package_ambiguity | EXISTING_PROBE_PARTIAL | EX19_SESSION_PACKAGE_PURCHASE | SPEC_READY | Current benchmark records new package rows but does not assert action_result/payment transaction count. |
| RM22 | Pulse capability scope | — | SPEC_BLOCKED | — | SPEC_BLOCKED | Product/QA scope gap: RM22 does not specify which Pulse operation is intended. |
| RM23 | Session-package refund quote | — | NEW_SCENARIO_REQUIRED | EX18_PACKAGE_BALANCE_REFUND_QUOTE | SPEC_READY | No benchmark scenario covers refund quote; current state snapshot does not count refund/payment rows. |
| RM24 | Schedule an AI follow-up | — | NEW_SCENARIO_REQUIRED | EX20_FOLLOW_UP_SCHEDULING | SPEC_READY | Current benchmark does not capture CRM task rows or assert due_at/source/dedupe fields. |
| RM25 | Marketing opt-out | — | NEW_SCENARIO_REQUIRED | EX21_MARKETING_OPTOUT | SPEC_READY | Current benchmark state snapshot does not include consent fields or activity events. |
| RM26 | Side question preserves active booking task | — | NEW_SCENARIO_REQUIRED | EX01_PRICE_AND_SIDE_READ | SPEC_READY | hypothetical_question and multi_request do not test a side read during an active booking. |
| RM27 | Urgent medical handoff | — | NEW_SCENARIO_REQUIRED | EX22_URGENT_MEDICAL_HANDOFF | SPEC_READY | Existing tests use a preclassified safety signal; they do not prove live semantic classification end-to-end. |
| RM28 | Complaint, payment dispute and privacy handoff | — | NEW_SCENARIO_REQUIRED | EX23_COMPLAINT_DISPUTE_PRIVACY_HANDOFF | SPEC_READY | No benchmark scenario covers these handoff types; exact persisted handoff fields are not captured. |
| RM29 | Out-of-scope request and recovery | out_of_scope_recovery | EXISTING_PROBE_PARTIAL | EX24_OUT_OF_SCOPE_RECOVERY | SPEC_READY | Existing scenario has no assertion for non-fabrication or successful in-scope recovery. |

## Proposed deduplicated execution templates

Templates are proposals only. Branch variants may require separate fixture runs so failures remain diagnosable.

| Template | RM IDs | Reused source scenarios | Purpose |
|---|---|---|---|
| EX01_PRICE_AND_SIDE_READ | RM01, RM26 | simple_price, service_price_comparison | Share the price side read with RM26; keep price and continuity assertions separate. |
| EX02_CLINIC_PAYMENT_READ | RM02 | new scenario | New read-only template; no existing benchmark scenario covers clinic/payment info. |
| EX03_DOCTOR_DISCOVERY_COMPARISON | RM03 | doctor_discovery, doctor_comparison | Use one template with separate discovery/comparison variants; assertions differ and must stay independently attributable. |
| EX04_NEAREST_AVAILABILITY | RM04 | nearest_available | Keep separate from conditional fallback; this is an unbounded next-available search. |
| EX05_EXPLICIT_TIME_SCOPE | RM05 | exact_time_availability, time_window_availability | Exact-time and range variants share the verified availability filter boundary but retain separate assertions. |
| EX06_CONDITIONAL_FALLBACK | RM06 | conditional_fallback | One template with branch variants. Existing anchor ensures non-empty primary and covers only skip-fallback. |
| EX07_SPOKEN_CLOCK_CONTEXT | RM07 | new scenario | No benchmark scenario tests bare spoken hour against clinic hours; existing deterministic tests define the branches. |
| EX08_STALE_CHOICE_REJECTION | RM08 | change_time_during_booking | Time-change probe is related but does not select an old option after invalidation. |
| EX09_SINGLE_BOOKING | RM09 | single_session_booking | Keep separate from grouped booking/reschedule; current source does not assert persisted row or response truth. |
| EX10_GROUPED_SAME_VISIT | RM10 | two_services_same_visit | Existing positive probe lacks assertions for group identity and atomicity. |
| EX11_SERVICE_REPLACEMENT | RM11 | change_service_mid_conversation | Distinct from adding/removing components in a grouped task. |
| EX12_GROUPED_SERVICE_ADD_REMOVE | RM12 | two_services_same_visit | Existing two-service scenario starts grouped; it does not exercise add/remove transitions. |
| EX13_TIME_CHANGE_DURING_BOOKING | RM13 | change_time_during_booking | Distinct from rescheduling an existing appointment: this changes a new booking task before its first write. |
| EX14_DEVICE_DOCTOR_COMPATIBILITY | RM14 | laser_device_consistency, two_services_same_visit | Reuse positive laser scenario; retain cardinality variants because they test different safety branches. |
| EX15_APPOINTMENT_READ_AMBIGUOUS_CANCEL | RM15, RM16 | cancel_existing | Share the list read with RM16; the verified candidate set supports the following ambiguity assertion. |
| EX16_UNAVAILABLE_RESCHEDULE | RM17 | reschedule_existing | Existing reschedule probe selects a valid replacement; a separate negative target is required. |
| EX17_PROFILE_AND_HISTORY_READ | RM18, RM19 | new scenario | Combine with RM19 as two turns for one patient; assertions remain separate. |
| EX18_PACKAGE_BALANCE_REFUND_QUOTE | RM20, RM23 | no_show_semantics | Share a safe owned-package fixture with RM23; retain no-show as a separate variant. |
| EX19_SESSION_PACKAGE_PURCHASE | RM21 | package_purchase, session_vs_package_ambiguity | Do not merge with Pulse purchase; they are distinct operation types. |
| EX20_FOLLOW_UP_SCHEDULING | RM24 | new scenario | No existing benchmark scenario covers follow-up; reuse native CRM scheduler. |
| EX21_MARKETING_OPTOUT | RM25 | new scenario | No benchmark scenario covers consent; existing executor/test defines narrow field-level write. |
| EX22_URGENT_MEDICAL_HANDOFF | RM27 | new scenario | No benchmark scenario exists; deterministic planner/responder tests define handoff behavior. |
| EX23_COMPLAINT_DISPUTE_PRIVACY_HANDOFF | RM28 | new scenario | One template group with three isolated variants; expected priorities remain separate. |
| EX24_OUT_OF_SCOPE_RECOVERY | RM29 | out_of_scope_recovery | Keep separate; recovery after out-of-scope request is not the same as a hypothetical or compound request. |

## Audit of all 26 existing benchmark scenarios

Every source is accounted for. scenario_level_semantic_assertions_present=false means the current core does not evaluate semantic pass/fail for that scenario.

| Existing scenario | RM relation | Decision | Why it is not full coverage |
|---|---|---|---|
| single_session_booking | RM09 | REUSE_PARTIAL | Positive booking probe; no assertion verifies persisted row or truthful status. |
| package_purchase | RM21 | REUSE_PARTIAL | Package purchase probe; action_result/payment boundary is not asserted. |
| session_vs_package_ambiguity | RM21 | REUSE_PARTIAL | Ambiguity path; expected no-write behavior is recorded but not evaluated. |
| existing_package_booking | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Books using owned package; distinct from read-only package balance. |
| existing_package_other_service | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Tests avoiding a mismatched existing package for another service; no direct RM ID captures this policy. |
| two_services_same_visit | RM10, RM12, RM14 | REUSE_PARTIAL | Positive grouped path; not add/remove transitions or all resource-cardinality branches. |
| exact_time_availability | RM05 | REUSE_PARTIAL | Known available exact time only; no unavailable-time assertion. |
| time_window_availability | RM05 | REUSE_PARTIAL | Range probe; no assertion checks all returned starts or gaps. |
| conditional_fallback | RM06 | REUSE_PARTIAL | Anchors a non-empty primary window; does not prove fallback activation after successful empty. |
| nearest_available | RM04 | REUSE_PARTIAL | No assertion proves returned option is truly earliest. |
| change_time_during_booking | RM13, RM08 | REUSE_PARTIAL | Tests time correction/booking, not stale-option rejection after scope invalidation. |
| change_doctor_during_booking | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Doctor-change continuation is distinct from doctor discovery/comparison. |
| change_service_mid_conversation | RM11 | REUSE_PARTIAL | Replacement read path has no final-state/dependency assertion. |
| doctor_discovery | RM03 | REUSE_PARTIAL | No doctor-set completeness assertion. |
| doctor_comparison | RM03 | REUSE_PARTIAL | No earliest verified availability ordering assertion. |
| laser_device_consistency | RM14 | REUSE_PARTIAL | Positive selected-device path only; no multi/zero-common-device branch. |
| simple_price | RM01 | REUSE_PARTIAL | Price-only probe has no price-equality assertion. |
| service_price_comparison | RM01 | REUSE_PARTIAL | Price/duration comparison has no exact fact-pair assertion. |
| package_comparison | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Offer comparison is distinct from purchasing the selected package. |
| hypothetical_question | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | No-write hypothetical question has no active task to preserve. |
| reschedule_existing | RM17 | REUSE_PARTIAL | Uses available replacement; does not cover unavailable target. |
| cancel_existing | RM15, RM16 | REUSE_PARTIAL | Seeds one target; does not prove complete list or ambiguity handling. |
| no_show_semantics | RM20 | REUSE_PARTIAL | Ground-truth package/no-show case is recorded but not asserted. |
| multi_request | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Compound multi-operation planning is not a side question during an active task. |
| out_of_scope_recovery | RM29 | REUSE_PARTIAL | No non-fabrication/recovery assertion. |
| two_packages_same_visit | Adjacent regression / no direct RM ID | KEEP_ADJACENT_REGRESSION | Two-package purchase plus grouped first visit is a distinct higher-risk integration path. |

## Required work before live execution

1. Implement an assertion evaluator for required_assertions; exception-free execution is not PASS.
2. Capture/query visit_group_id for grouped appointments and target appointment fields before/after cancellation/rescheduling.
3. Capture/query CRM task fields, Pulse pack/payment/refund rows, marketing-consent fields and activity events.
4. Keep fixture prices, doctors, services, dates, devices and unavailable times data-driven.
5. Resolve RM22 scope before adding an execution group.
6. Keep live fail-closed. Do not change Grouped Live Review assertions or Production Logic.
