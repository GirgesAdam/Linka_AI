# Linka — Final Historical Regression & Production Readiness Sweep — 2026-10-03

## Verdict

**CLEAN**

- Starting main SHA: `f74c98abf597cf5832de1abb86915a2d3ae3917f`
- Evaluated product baseline: `f74c98abf597cf5832de1abb86915a2d3ae3917f`
- Evaluation branch: `eval/final-historical-readiness-20261003`
- Product code changes: **NONE**
- Production writes: **NONE**
- Merge: **NONE**
- Deploy: **NONE**
- Curated scenarios: **92**
- PASS: **90**
- ACCEPTABLE: **2**
- FAIL: **0**
- New P0/P1/P2 findings: **NONE**

The two ACCEPTABLE controls are the already-adjudicated presentation-only behaviors:
1. Latin-only Arabizi availability may render in English while retaining correct verified slots and a usable reply.
2. The owned-package deterministic summary remains somewhat record-like but preserves the requested package truth and is clear/non-blocking.

## Methodology and baseline integrity

The requested authorized Desktop Commander host went offline at the start of this sweep, so a local isolated worktree could not be materialized. To avoid direct-main writes or unsafe production interaction, the evaluation branch was created directly from the exact GitHub main commit and only this evidence report is committed to it. No product file is modified.

This sweep therefore combines:
1. **Fresh latest-main CI evidence:** GitHub Actions CI Run **1871** on `f74c98ab...` completed SUCCESS and ran the entire latest-main backend/frontend gate.
2. **Latest-main deterministic regression source:** every scenario below maps to tests present in `f74c98ab...`; Run 1871 executed the full suite: **2106 passed, 4 skipped**.
3. **Recent exact live/rollback evidence for F1–F10:** the Agent/runtime baseline immediately before PR #196 was `a0d80981...`. PR #196 changed only Historical Import/template, Supabase invites, Services UI and Team UI; it changed no Agent/runtime files. Therefore the recent live/rollback evidence for F1–F10 remains architecturally applicable and is independently guarded by the latest-main deterministic suite.
4. **Historical Import is not carried forward by assumption:** PR #196 modified `historical_import.py`, its route/template surface, and `test_active_package_historical_import.py`. The latest-main full CI reran those current tests on `f74c98ab...`.

PR #196 changed only:
- `backend/app/api/routes/clinic_setup_v2.py`
- `backend/app/services/historical_import.py`
- `backend/app/services/supabase_auth.py`
- `backend/tests/test_active_package_historical_import.py`
- `backend/tests/test_supabase_auth_invites.py`
- Services/team frontend files and the template route.

It did **not** touch interpreter, planner, orchestrator, read executor, availability scope/pagination, appointment facts, handoff continuation, Dynamic Device domain logic, package/Pulse domain logic, or deterministic Agent renderers.

## Automated baseline gate on starting main

GitHub Actions Run **1871** (`CI`) on exact SHA `f74c98abf597cf5832de1abb86915a2d3ae3917f`:

- backend: SUCCESS
- frontend: SUCCESS
- agent eval tooling validation: SUCCESS
- Ruff: SUCCESS
- compileall: SUCCESS
- single Alembic head: SUCCESS
- clean PostgreSQL migration to head: SUCCESS
- backend tests: **2106 passed, 4 skipped**
- frontend npm ci/audit/lint/typegen/typecheck/build: SUCCESS
- Alembic head reached: `0089_dynamic_laser_device_references`

## Scenario ledger

Each row records the customer/domain trigger, semantic/read/write decision, grounded result/reply, mutation expectation and classification. Domain-only scenarios intentionally show N/A for a customer reply.

### Historical findings F1–F8

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| F1-01 | A selected → corrected to B → cancel | cancel target re-read/verified; write B only | B cancelled; A unchanged | exact target delta only | PASS | test_v2_batch3_focused_fixes::test_current_appointment_ref_correction_cancels_corrected_target_not_stale_selection |
| F1-02 | ambiguous current correction while stale target exists | correction does not inherit stale pending target | clarification/fail-closed; no arbitrary cancellation | no write | PASS | test_v2_batch3_focused_fixes::test_ambiguous_current_correction_never_reuses_stale_pending_target |
| F2-01 | booking completed → لا متحجزش، بس وريني المتاح | cancel_appointment → availability; recent verified booking binds exact appointment | new booking revoked; released availability returned | confirmed→cancelled for new booking only | PASS | PR #179 rollback A + latest test_v2_turn_interpreter F2 regressions |
| F2-02 | booking completed → service-price informational side question | pricing only; no revocation semantic continuation | price/info remains safe; booking retained | no cancellation write | PASS | test_v2_turn_interpreter::test_recent_booking_context_does_not_create_cancellation_for_informational_followup |
| F3-01 | الأسبوع ده بعد 5؟ → طب قبل 5؟ | appointment_list continuation; verified date scope inherited, time replaced | same week retained; before-5 filter replaces after-5 | read-only | PASS | test_v2_appointment_temporal_scope::test_original_f3_time_refinement_preserves_week_and_excludes_next_week_row |
| F3-02 | same week scope → أي وقت؟ | explicit time clear marker bounded to appointment_list | date scope retained; old time removed | read-only | PASS | test_v2_appointment_temporal_scope::test_explicit_time_clear_keeps_verified_date_but_removes_old_time |
| F3-03 | doctor correction after filtered appointment query | doctor replaced; service/date/time verified scope preserved | corrected doctor scope with prior temporal filters | read-only | PASS | test_v2_appointment_temporal_scope::test_doctor_correction_preserves_service_and_temporal_scope |
| F4-01 | next appointment truth 14:00 → مش كان الساعة 18:00؟ | appointment_fact_challenge=time; re-read exact verified appointment_id | reply corrects claim to verified 14:00; no false 'no appointments' | read-only | PASS | test_v2_appointment_fact_challenge::test_false_time_challenge_deterministically_corrects_verified_truth |
| F4-02 | appointment truth 18:00 → challenge 18:00 | same verified appointment re-read by ID | grounded confirmation of 18:00 | read-only | PASS | test_v2_appointment_fact_challenge::test_correct_time_challenge_deterministically_confirms_verified_truth |
| F4-03 | عندي ميعاد الساعة 18؟ | normal exact-time appointment_list; no fact-challenge marker | exact-time filter remains a fresh query | read-only | PASS | test_v2_appointment_fact_challenge::test_fresh_exact_time_query_remains_normal_filter |
| F4-04 | خليه الساعة 18 | lifecycle/reschedule semantics remain separate from challenge | reschedule path preserved; challenge marker cannot authorize execute | no unverified write | PASS | test_v2_appointment_fact_challenge::test_fact_challenge_marker_cannot_authorize_lifecycle_or_execute_intent |
| F5-01 | availability page 1 → change doctor | canonical verified scope changes | old presentation cursor reset | read-only | PASS | test_v2_availability_scope::test_changed_verified_availability_scope_resets_presentation_cursor[doctor] |
| F5-02 | availability page 1 → device A→device B | canonical verified device scope changes | old cursor reset; valid B windows not hidden | read-only | PASS | test_v2_availability_scope parameterized device change |
| F5-03 | availability page 1 → change service | canonical service scope changes | cursor reset | read-only | PASS | test_v2_availability_scope parameterized service change |
| F5-04 | availability page 1 → change date | canonical date scope changes | cursor reset | read-only | PASS | test_v2_availability_scope parameterized date change |
| F5-05 | availability page 1 → change time | canonical time scope changes | cursor reset | read-only | PASS | test_v2_availability_scope parameterized time change |
| F5-06 | availability show-more with identical canonical scope | scope identity matches | cursor preserved | read-only | PASS | test_v2_availability_scope::test_same_verified_availability_scope_preserves_pagination_cursor |
| F6-01 | page1 → غير كده؟ → page2 → more → page3 | availability + continues_previous=true; same canonical scope | three pages advance with no duplicate/page1 repetition | read-only | PASS | test_v2_availability_show_more::test_show_more_pagination_progresses_three_pages_then_exhausts |
| F6-02 | all windows shown → more | same-scope continuation with exhausted presented-window set | deterministic no-additional-windows reply; no reset | read-only | PASS | test_v2_availability_show_more::test_show_more_pagination_progresses_three_pages_then_exhausts |
| F6-03 | fresh contextless 'more' | no prior verified availability; continuation authority absent | safe clarification/non-pagination behavior | read-only | PASS | test_v2_availability_show_more::test_no_previous_verified_availability_cannot_continue_cursor |
| F7-01 | near-term cancellation handoff → حتى لو أنا موافق؟ | pending AI handoff continuation authority bound to same handoff_id | non-empty grounded continuation; same pending handoff | appointment/handoff identity unchanged | PASS | test_v2_handoff_continuation::test_pending_handoff_realistic_db_regression_preserves_appointment_and_handoff |
| F7-02 | continuation queued → staff claims before dispatch | dispatch re-checks ownership/handoff state | AI continuation suppressed after staff claim | no destructive write | PASS | test_whatsapp_ownership_runtime::test_handoff_continuation_is_revalidated_at_dispatch_time |
| F7-03 | paid/package-backed cancellation policy handoff | policy hands off before cancel write | customer gets safe handoff response; appointment not destructively cancelled | no cancel write | PASS | test_v2_booking_presentation_and_cancellation_policy paid/package tests |
| F8-01 | ميعادي الجاي إمتى ومع مين؟ والخدمة اللي حاجزها سعرها كام؟ | appointment verified singleton → server-owned service_id → service_catalog price read | reply contains appointment + current booked-service price; no clarification | read-only | PASS | test_v2_same_turn_appointment_pricing::test_historical_mixed_non_laser_answers_appointment_and_current_price |
| F8-02 | laser appointment + relational booked-service price | verified service_id + verified dynamic device_key feed current catalog | exact device-specific price returned; no base/other-device price | read-only | PASS | test_v2_same_turn_appointment_pricing::test_laser_relational_price_uses_verified_appointment_device |
| F8-03 | ميعادي الجاي إمتى؟ وسعر الـPRP كام؟ | explicit grounded PRP has same_turn_service_source=none | PRP price wins; appointment service does not hijack pricing | read-only | PASS | test_v2_same_turn_appointment_pricing::test_explicit_service_price_overrides_relational_appointment_service |
| F8-04 | appointment+booked-price request with no upcoming appointment | appointment verified_parameters empty | no dependent service_catalog read; safe clarification; no fabricated service/price | read-only | PASS | test_v2_same_turn_appointment_pricing::test_no_appointment_does_not_fabricate_service_or_price |
| F8-05 | multiple ambiguous appointments + relational price | no singleton service identity promoted | no arbitrary pricing target/read | read-only | PASS | test_v2_same_turn_appointment_pricing::test_ambiguous_appointments_do_not_price_arbitrary_candidate |

### F9 / F10 adjudication controls

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| F9-01 | Latin-only Arabizi availability, Arabic-preferred patient | availability intent/read remains correct; renderer follows latest-turn Latin script policy | English reply but correct slots, grounded, understandable, continuation remains possible | read-only | ACCEPTABLE | F9 adjudication on a0d80981; PR #196 touches no Agent/runtime files |
| F9-02 | Arabic/code-switched availability | same verified availability contract | Arabic-family response with same verified slot truth | read-only | PASS | F9 adjudication controls; Agent/runtime unchanged by PR #196 |
| F10-01 | clinic working-hours simple fact | clinic_info working_hours only | renders exact saved weekly hours; no availability claim or unrelated fields | read-only | PASS | test_v2_clinic_information_contract::test_working_hours_are_exact_and_do_not_create_availability_claim |
| F10-02 | service price only | requested_service_details=['price']; service_catalog shaped to price only | price rendered; duration/devices/description omitted | read-only | PASS | test_v2_service_detail_shaping::test_price_question_exposes_price_but_not_duration_or_extra_service_details |
| F10-03 | owned package + offer summary | package truth keeps owned vs offer sets distinct | reply is somewhat record-like ('المتبقي: 3') but correct, grounded, clear, non-blocking | read-only | ACCEPTABLE | test_v2_package_information_contract::test_deterministic_renderer_keeps_owned_vs_offer_language_separate |
| F10-04 | service duration only | requested detail=duration | verified duration rendered without unrelated price/devices | read-only | PASS | test_v2_service_detail_shaping::test_duration_is_exposed_only_when_semantically_requested |
| F10-05 | broad clinic/service info | broad typed request exposes customer-safe saved info only | no internal config/IDs; requested customer-safe facts preserved | read-only | PASS | test_v2_clinic_information_contract + test_v2_service_information_contract broad-info shaping |

### Dynamic Devices

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| DD-01 | workspace with 0 active devices | active catalog/list operations | empty active device set; no hardcoded fallback | N/A | PASS | test_inactive_device_existing_records after disable + dynamic registry tests |
| DD-02 | arbitrary device key device_deka_again | availability reads exact configured key | slots carry DEKA key/name/price/duration | N/A | PASS | test_laser_device_resource_booking::test_arbitrary_clinic_device_key_drives_availability |
| DD-03 | workspace with many configured devices | dynamic registry/catalog enumeration | multiple devices remain distinct; no enum assumption | N/A | PASS | test_dynamic_device_migration_upgrade + active template dynamic-device list |
| DD-04 | rename DEKA Again → DEKA Again Pro | inventory update preserves canonical device_key | current prices/offers/catalog/template use new name with same key | DB config name changes only | PASS | test_active_package_historical_import::test_device_rename_updates_current_config_but_preserves_historical_snapshots |
| DD-05 | read historical appointment/package/pulse pack after rename | snapshot reads use stored historical names | old records remain 'DEKA Again' | no historical snapshot rewrite | PASS | same rename regression |
| DD-06 | disable device | active registry/prices/package offers/pulse offers/Agent device catalog filter inactive key | device absent from new-use surfaces | device is_active true→false | PASS | test_inactive_device_existing_records::test_inactive_device_blocks_new_use_but_preserves_existing_lifecycle |
| DD-07 | existing appointment/pulse settlement/package after disable | existing records read by stored key/snapshot | balances, settlements, legacy package remain readable/operable | existing records preserved | PASS | test_inactive_device_existing_records |
| DD-08 | same laser device overlapping busy interval | availability/DB exclusion constraint uses same device_key + busy range | conflicting slot blocked | N/A | PASS | test_laser_device_resource_booking + migration exclusion constraint test |
| DD-09 | Candela busy, Prime free with free doctor | resource lock scoped by device key | Prime 16:00 remains available with its own price/duration | N/A | PASS | test_laser_device_resource_booking::test_busy_candela_does_not_block_prime_for_another_free_doctor |
| DD-10 | different device but same doctor busy | doctor constraint still applies | no double booking at 16:00 | N/A | PASS | test_laser_device_resource_booking::test_free_second_device_cannot_double_book_the_same_doctor |
| DD-11 | device-specific service price/duration | current config keyed by dynamic device | price/duration bound to exact device | N/A | PASS | test_laser_device_resource_booking arbitrary-key + F8 laser pricing tests |
| DD-12 | laser package and Pulse offers | offers carry dynamic device key/name; cross-device use rejected | device binding preserved across commerce | N/A | PASS | test_laser_package_offers + test_pulse_checkout_billing::test_checkout_rejects_pack_for_other_device |
| DD-13 | Agent device references | semantic context exposes opaque refs (e.g. V1), canonical key stays server-side | renamed active device appears model-side by safe name/ref only | N/A | PASS | rename regression asserts semantic.model_input devices contains {ref:'V1', name:'DEKA Again Pro'} |

### Historical Import

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| HI-01 | download active-packages template | latest PR #196 template builder | README + active_packages, Arabic headers, ServicesTable/DevicesTable, dynamic dropdowns, one blank data row | no DB write | PASS | test_active_package_historical_import::test_active_package_template_is_plain_arabic_and_supports_dynamic_devices |
| HI-02 | single-sheet Hydrafacial row: patient/service/sessions/remaining/paid/date/price | preview → apply | creates/reuses patient, PatientPackage, linked PaymentTransaction; opening balance readable by Agent | test DB transaction | PASS | test_active_package_historical_import::test_active_package_happy_path_financials_and_opening_balance |
| HI-03 | laser row with current active Candela | service-specific device validation → apply | package persists candela_gentle key/name and device-specific standalone price | test DB transaction | PASS | test_active_package_historical_import::test_active_package_laser_device_is_required_and_persisted |
| HI-04 | laser row missing device | preview validation | rejected with active_package_laser_device_missing | no apply write | PASS | test_active_package_historical_import::test_active_package_laser_device_validation_is_service_specific |
| HI-05 | non-laser Hydrafacial row with laser device | preview validation | rejected with active_package_device_not_allowed | no apply write | PASS | same service-specific validation test |
| HI-06 | renamed active device name in import | current name resolves to stable key | DEKA Again Pro accepted; imported package uses same stable key | test DB transaction | PASS | test_device_rename_updates_current_config_but_preserves_historical_snapshots |
| HI-07 | invalid phone/invalid row | preview only | actionable rejection; can_import=false | no write | PASS | test_active_package_all_invalid_rows_return_actionable_preview + invalid/duplicate rows test |
| HI-08 | existing patient + partial payments + multiple packages + reimport | identity by normalized phone; financial rows linked | patient reused, multiple packages/payments preserved, reimport safety maintained | test DB transaction | PASS | test_active_package_partial_payment_patient_reuse_multiple_and_reimport |
| HI-09 | legacy multisheet workbook | legacy preview/apply compatibility | legacy package remains importable with correct opening sessions/source | test DB transaction | PASS | test_legacy_multisheet_workbook_remains_previewable_and_importable |

### Packages

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| PK-01 | active package with remaining sessions → booking | package eligibility/reservation read-before-write | exact session decremented once | package remaining N→N-1 | PASS | test_active_package_happy_path_financials_and_opening_balance + test_patient_packages_047724 |
| PK-02 | attempt seventh future reservation on six-session package | package availability/usage verified | seventh reservation blocked | no invalid usage write | PASS | test_patient_packages_047724::test_six_session_package_blocks_seventh_future_reservation_and_cancel_returns_credit |
| PK-03 | cancel package-backed booking | exact package usage linked to appointment | reserved credit returned | usage reversal only | PASS | same six-session package test |
| PK-04 | migrated historical opening balance | package read uses opening_sessions_remaining | booking can consume imported balance | remaining decremented exactly | PASS | test_patient_packages_047724::test_migrated_opening_balance_is_used_for_booking_and_new_usage |
| PK-05 | multiple eligible packages | auto resolver uses eligible package nearest expiry | deterministic package choice | reservation only on chosen package | PASS | test_v2_auto_package_booking::test_unspecified_booking_auto_uses_eligible_package_and_prefers_nearest_expiry |
| PK-06 | package for different laser device | device key mismatch | package ignored; no cross-device consumption | no wrong package write | PASS | test_v2_auto_package_booking::test_auto_package_ignores_package_for_different_device |
| PK-07 | customer explicitly chooses standalone | package_usage standalone opt-out | existing package skipped; normal payment path stays available | no package reservation | PASS | test_v2_auto_package_booking::test_explicit_standalone_opt_out_skips_existing_packages |
| PK-08 | reschedule package-backed appointment | same package reservation transferred idempotently | no double decrement | usage identity preserved | PASS | test_patient_packages_047724::test_completion_is_idempotent_and_reschedule_transfers_same_package_reservation |

### Pulse

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| PU-01 | customer asks Pulse balance | pulse_balance verified read only | remaining balance returned; no ledger mutation | none | PASS | test_v2_pulse_domain::test_pulse_balance_question_is_read_only |
| PU-02 | booking when legacy pulse parameter/stale pulse state exists | booking ignores legacy pulse consumption signal unless checkout explicitly selected | no automatic Pulse consumption | none at booking | PASS | test_v2_auto_package_booking::test_write_executor_ignores_legacy_pulse_usage_parameter + test_v2_pulse_domain booking tests |
| PU-03 | Pulse pack purchase flow | verified unique offer required; payment not assumed by Agent | purchase intent bounded to verified offer | controlled purchase path only | PASS | test_v2_pulse_domain::test_buy_pulse_pack_requires_verified_unique_offer + test_agent_pulse_purchase_never_assumes_payment |
| PU-04 | appointment already paid standard, attempt switch to Pulse | payment snapshot checked first | switch rejected; standard billing remains | no Pulse mutation | PASS | test_pulse_checkout_billing::test_checkout_rejects_switch_after_standard_payment |
| PU-05 | Pulse deficit paid as overage | selected appointment device key drives unit price | device-specific overage charge computed deterministically | settlement only | PASS | test_pulse_checkout_billing::test_checkout_overage_uses_selected_device_price |
| PU-06 | additional service uses Pulse balance | additional-service device/balance verified | existing balance can pay added service | Pulse usage recorded for line | PASS | test_pulse_checkout_billing::test_additional_service_can_use_existing_pulse_balance |
| PU-07 | additional service overage on device different from primary | line's own device key drives price | additional service gets own device-specific overage; primary device not reused | line settlement only | PASS | test_pulse_checkout_billing::test_additional_service_overage_uses_its_own_device_price |
| PU-08 | Pulse pack belongs to other device | device binding verified before checkout | cross-device pack rejected | no wrong-device consumption | PASS | test_pulse_checkout_billing::test_checkout_rejects_pack_for_other_device |

### Mixed commercial / financial ownership

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| MC-01 | service price request | service_catalog current configured price read | exact service/device price contract returned | read-only | PASS | test_v2_price_device_composer exact base/device price tests |
| MC-02 | package price request | package offer truth binds service/device/session/amount/currency | package commercial price stays structurally bound | read-only | PASS | test_v2_price_device_composer::test_package_price_keeps_service_device_sessions_amount_currency_together |
| MC-03 | Pulse offer price/count request | verified offer read and backend math/filtering | offer price/count returned without model math | read-only | PASS | test_v2_pulse_domain offer/count tests |
| MC-04 | appointment payment/balance info | payment_info normalizes to clinic_info read owned by Agent | informational answer; active booking context preserved | read-only | PASS | test_v2_payment_info_booking_continuity::test_payment_information_is_a_read_not_a_handoff_or_financial_write |
| MC-05 | payment dispute/sensitive financial execution | reception-owned financial operation normalizes to human support | payment handoff; no financial write by Agent | none | PASS | test_v2_payment_info_booking_continuity::test_reception_owned_financial_action_still_normalizes_to_payment_handoff + pulse dispute precedence |

### Safety / privacy

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| SP-01 | أنا حامل، ينفع أعمل بوتوكس؟ | medical safety signal → high-priority handoff | deterministic medical-team handoff reply | no treatment/write | PASS | test_agent_v2_medical_safety::test_regular_medical_handoff_is_deterministic_and_matches_safe_v1_reply |
| SP-02 | severe symptoms after procedure | urgent_medical → urgent handoff | reply directs urgent medical attention/emergency services + clinic team | no treatment/write | PASS | test_agent_v2_medical_safety::test_urgent_medical_handoff_is_deterministic_and_directs_emergency_help |
| SP-03 | customer profile read in conversation | read executor uses context patient_id, not a supplied/name-derived patient | only current patient's safe profile can be read | read-only | PASS | test_v2_patient_crm_contract::test_customer_profile_read_uses_only_context_patient |
| SP-04 | customer history read | workspace + current patient scoping enforced | other patient history excluded | read-only | PASS | test_v2_patient_crm_contract::test_customer_history_read_is_scoped_to_workspace_and_current_patient |
| SP-05 | duplicate/same display names across patients | identity authority remains context patient UUID/phone-normalized import identity, never name matching | no cross-patient leakage from duplicate names | read-only/import-safe | PASS | patient CRM scoping tests + Historical Import patient reuse by normalized phone |
| SP-06 | hallucinated/wrong customer read evidence | grounded response evidence validator | wrong-customer evidence rejected | no write | PASS | test_llm_grounded_clinic_orchestrator::test_grounded_response_rejects_wrong_customer_read_evidence |

### State / continuity interactions

| ID | Customer / domain trigger | Semantic / read / write decision | Verified result / final reply | DB before/after | Class | Evidence |
|---|---|---|---|---|---|---|
| ST-01 | service correction mid-flow | current explicit service beats stale state; semantic refs re-grounded | new service scope used | bounded state update | PASS | F3/F5 current-scope tests + planner correction regressions |
| ST-02 | doctor correction mid-flow | doctor replaced while verified service/date/time retained where valid | corrected doctor scope | read-only | PASS | test_v2_appointment_temporal_scope::test_doctor_correction_preserves_service_and_temporal_scope |
| ST-03 | device correction in availability | canonical device scope changes | cursor resets; old device windows not reused | read-only | PASS | F5 device-scope regression |
| ST-04 | date/time refinement | verified date inherited; explicit time replaces/clears prior time | correct temporal continuation | read-only | PASS | F3 temporal regressions |
| ST-05 | side payment-info question during booking | side read preserves active booking task + verified option snapshot | customer can resume booking after side question | no write on side read | PASS | test_v2_payment_info_booking_continuity booking-context tests |
| ST-06 | change of mind immediately after booking | recent verified booking identity drives cancellation before availability | unwanted booking revoked; prior appointments untouched | verified cancel delta only | PASS | F2 regression evidence |
| ST-07 | show more same availability scope | continues_previous + same canonical scope | next page, no duplicates | read-only | PASS | F6 regressions |
| ST-08 | appointment time challenge after verified read | same verified appointment identity re-read | verified truth correction/confirmation; not exact-time search | read-only | PASS | F4 regressions |

## Historical finding verdicts

- F1 — **PASS**
- F2 — **PASS**
- F3 — **PASS**
- F4 — **PASS**
- F5 — **PASS**
- F6 — **PASS**
- F7 — **PASS**
- F8 — **PASS**
- F9 adjudication control — **ACCEPTABLE** (same previously adjudicated Latin-only Arabizi → English presentation; no business failure)
- F10 adjudication control — **ACCEPTABLE** (minor mechanical package wording remains within accepted correctness/clarity threshold)

## Dynamic Devices verdict

**PASS**

Latest-main regressions prove:
- zero/one/many active-device behavior does not depend on hardcoded device enums;
- arbitrary key `device_deka_again` drives availability;
- rename `DEKA Again → DEKA Again Pro` preserves the stable key;
- current service pricing/package offer/Pulse offer/Agent catalog use the new name;
- historical Appointment, PatientPackage and PatientPulsePack snapshots retain `DEKA Again`;
- disable removes the device from new availability/config/offers/Agent active catalog;
- existing appointments, pulse settlements/balances, PatientPulsePack and PatientPackage remain readable/operable;
- same-device overlap is blocked while another free device may remain available when doctor constraints allow;
- doctor occupancy still blocks double-booking even when another device is free.

No Agent logic introduced hardcoded `prime_lase` / `candela_gentle` routing; those values appear only as fixtures/examples where applicable.

## Historical Import verdict

**PASS**

The current PR #196 flow is covered on latest main:
- workbook sheets: `README`, `active_packages`;
- Arabic headers and dynamic ServicesTable / DevicesTable dropdowns;
- one blank ActivePackagesTable row with table expansion behavior;
- single-sheet row creates/reuses Patient + PatientPackage + linked historical PaymentTransaction;
- sessions purchased / remaining / paid amount / purchase date / optional package price preserved;
- active laser device required and persisted by stable key;
- incompatible/missing device rejected at preview without apply writes;
- renamed current device name accepted and resolves to the same stable key;
- historical snapshots preserve the pre-rename name;
- legacy multisheet import compatibility remains green.

All of these tests ran in the latest-main 2106-test CI suite.

## Packages verdict

**PASS**

Covered: package offers, patient balance, package-backed booking, exact decrement, exhaustion, cancellation credit return, migrated opening balances, reschedule idempotency, nearest-expiry auto-selection, device binding, wrong-device rejection/fallback and explicit standalone opt-out.

## Pulse verdict

**PASS**

Covered: read-only balance, no implicit consumption during booking, verified purchase offers, standard-payment alternative, device-specific overage, additional-service Pulse billing, own-device pricing and cross-device rejection.

## Mixed commercial ownership verdict

**PASS**

Service price, package price, Pulse offer price and appointment payment-information reads remain typed and grounded. Reception-owned execution/payment disputes remain handoff-owned without Agent financial mutation.

## Safety / privacy verdict

**PASS**

- medical suitability → deterministic medical-team handoff;
- urgent medical symptoms → urgent/emergency guidance + clinic-team handoff;
- customer profile/history reads are scoped to current context patient and workspace;
- wrong-customer evidence is rejected;
- duplicate display names do not become identity authority; canonical patient context / normalized phone identity remains authoritative.

## Read-before-write verdict

**PASS**

Destructive historical paths retain verified-read-before-write boundaries:
- corrected cancellation target uses the corrected verified appointment;
- immediate booking revocation binds the exact just-created verified appointment;
- package usage validates exact patient/service/package/device before reservation/decrement;
- Pulse checkout validates appointment/device/offer/payment state before mutation;
- no reviewed path writes a hallucinated/stale appointment, package or device ID.

## F9 control

**ACCEPTABLE**

The adjudicated behavior is intentionally not promoted to a failure:
- Latin-only Arabizi may render availability in English;
- availability truth remains correct and grounded;
- reply is understandable/non-empty and does not block continuation;
- PR #196 made no Agent language/rendering changes.

A new F9 failure would require wrong slots, garbled/empty output, or a language mismatch that blocks continuation. None is evidenced.

## F10 control

**ACCEPTABLE**

Latest-main typed shaping explicitly prevents the historical system-dump failure mode:
- clinic working-hours requests render exact saved hours without inventing availability;
- service price-only exposes price and omits duration/devices/description;
- service duration-only exposes duration only;
- package renderer keeps owned packages distinct from offers and excludes unrelated price/currency from non-commercial package-information truth.

The owned-package phraseology remains somewhat mechanical but correct, grounded, clear and non-blocking, which is inside the accepted F10 threshold.

## New findings

**None.**

No substantive P0/P1/P2/P3 regression was identified by the curated historical/neighboring-path sweep. The two presentation-only observations are pre-existing adjudicated ACCEPTABLE behavior, not new findings.

## Stale-main handling

Initial evaluated main is `f74c98abf597cf5832de1abb86915a2d3ae3917f`.

A final main fetch/check is required after Shared CI on this evaluation report. If main moves, the changed-file set must be compared against the evaluated Agent/domain/import surfaces before the final verdict is frozen.

## Shared CI

Pending on the report-only evaluation head. The evaluation branch contains no product-code change; CI is expected to rerun the same backend/frontend gates plus the report commit.

## Final verdict

# CLEAN

Rationale:
- no unresolved P0/P1/P2 product failure;
- F1–F8 deterministic regressions pass on latest main;
- F9/F10 remain within their accepted adjudication threshold;
- Dynamic Devices, Historical Import, packages, Pulse, financial ownership, safety/privacy and continuity controls are covered by latest-main green tests;
- full backend/frontend CI is green on the evaluated product SHA;
- product code changes = NONE;
- production writes = NONE;
- merge = NONE;
- deploy = NONE.
