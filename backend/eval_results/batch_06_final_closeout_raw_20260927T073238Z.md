# Tia Agent Evaluation — Batch 06 Final Closeout

- Runtime base SHA: fe6ab2f2d407808b235f16e3718de11ed613c803
- Evaluation tooling SHA: fe6ab2f2d407808b235f16e3718de11ed613c803
- Scenario version: batch6-v1
- Model: gpt-5.6-luna
- Reasoning: low
- Scenarios executed: 16

Raw deterministic findings are guards; final verdict requires manual DB/write/response review.

## b6_01_two_service_same_visit_success

Category: grouped_atomicity
Purpose: Two canonical services requested as one visit must create one sequential logical visit.
Review status: PENDING_MANUAL_REVIEW
Expected: Exactly two sequential appointments share one visit_group_id with no extra appointment.

Customer 1: احجزيلي هيدرافيشل وتنظيف بشرة عميق في نفس الزيارة يوم 2026-09-28 الساعة 10:00 مع مريم حسن، ورا بعض
Tia: تم حجز هيدرافيشل يوم 28 سبتمبر الساعة 10:00، وبعده تنظيف بشرة عميق الساعة 11:15، مع مريم حسن في نفس الزيارة. السعر الإجمالي 3000 جنيه.
Usage: in=8456 read=5430 write=2252 uncached=774 out=580 latency=15794ms

DB verification:
{
  "created": [
    {
      "id": "028b8665-cd9a-4441-ac72-0212f633fc70",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "6381007a-d086-58da-a983-4d56ab690240",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "b68dbb69-5ccb-45c0-9987-a8a4c4d9b22b",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "6381007a-d086-58da-a983-4d56ab690240",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "028b8665-cd9a-4441-ac72-0212f633fc70",
        "b68dbb69-5ccb-45c0-9987-a8a4c4d9b22b"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_02_second_component_unavailable

Category: grouped_atomicity
Purpose: A newly unavailable second component must suppress the whole grouped commit.
Review status: PENDING_MANUAL_REVIEW
Expected: Fresh verification detects the second-component conflict and creates zero customer appointments.

Customer 1: هل هيدرافيشل وتنظيف بشرة عميق متاحين في نفس الزيارة يوم 2026-09-28 الساعة 10:00 مع مريم حسن؟
Tia: أيوه، هيدرافيشل وتنظيف بشرة عميق متاحين يوم 28 سبتمبر 2026 الساعة 10:00 مع مريم حسن. سعر الهيدرافيشل 1800 جنيه، وتنظيف البشرة العميق 1200 جنيه.
Usage: in=7853 read=5430 write=1652 uncached=771 out=509 latency=8968ms

Customer 2: تمام احجزيهم كده
Tia: المتاح يوم 28 سبتمبر هو الهيدرافيشل الساعة 12:15 مع مريم حسن، وتنظيف البشرة العميق الساعة 13:30 مع مريم حسن. تحبي أحجز كل خدمة في موعدها؟
Usage: in=8346 read=5430 write=1889 uncached=1027 out=777 latency=15974ms

DB verification:
{
  "requested_anchor": "2026-09-28T07:00:00+00:00",
  "blocked_second_start": "2026-09-28T08:15:00+00:00",
  "competing_appointment_id": "36826858-ac2a-4ac1-8d9b-6dec42819fba",
  "created_for_customer": [],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_03_component_needs_device_clarification

Category: grouped_atomicity
Purpose: A missing laser device must block every component until the customer chooses a grounded device.
Review status: PENDING_MANUAL_REVIEW
Expected: Clarify the missing laser device with grounded options and zero partial booking.

Customer 1: احجزيلي هيدرافيشل وليزر إزالة الشعر - إبط في نفس الزيارة يوم 2026-09-28 الساعة 12:00 مع أحمد محمود، ورا بعض
Tia: تمام، أقدر أرتّب الهيدرافيشل وليزر إزالة الشعر - إبط ورا بعض في نفس الزيارة يوم 28 سبتمبر الساعة 12:00 مع أحمد محمود، لكن محتاجة تختاري جهاز الليزر: Candela Gentle بسعر 550 جنيه أو Prime Lase بسعر 650 جنيه؟
Usage: in=7950 read=5430 write=1747 uncached=773 out=584 latency=9313ms

DB verification:
{
  "created": [],
  "response": "تمام، أقدر أرتّب الهيدرافيشل وليزر إزالة الشعر - إبط ورا بعض في نفس الزيارة يوم 28 سبتمبر الساعة 12:00 مع أحمد محمود، لكن محتاجة تختاري جهاز الليزر: Candela Gentle بسعر 550 جنيه أو Prime Lase بسعر 650 جنيه؟",
  "plan_steps": [
    {
      "operation_index": 0,
      "operation_type": "book",
      "disposition": "blocked",
      "reads": [],
      "write_intent": null,
      "state_action": "none",
      "response_goal": "clarification",
      "clarification_field": null,
      "facts": {
        "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
        "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
        "date": {
          "mode": "exact",
          "start_date": "2026-09-28",
          "end_date": null
        },
        "time": {
          "mode": "exact",
          "start_time": "12:00",
          "end_time": null,
          "start_time_ambiguity": "none",
          "end_time_ambiguity": "none"
        },
        "package_usage": "unspecified",
        "exact_time_requested": true,
        "service_requires_laser_device": false,
        "compound_write_group": "compound:0,1",
        "compound_visit_group": "compound:0,1",
        "compound_visit_grouped": true,
        "compound_visit_preflight_resolved": true,
        "compound_visit_missing_device": true
      }
    },
    {
      "operation_index": 1,
      "operation_type": "book",
      "disposition": "clarify",
      "reads": [
        {
          "kind": "availability",
          "parameters": {
            "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
            "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
            "date": {
              "mode": "exact",
              "start_date": "2026-09-28",
              "end_date": null
            },
            "time": {
              "mode": "nearest",
              "start_time": "12:00",
              "end_time": null,
              "start_time_ambiguity": "none",
              "end_time_ambiguity": "none"
            },
            "package_usage": "unspecified"
          }
        }
      ],
      "write_intent": null,
      "state_action": "none",
      "response_goal": "clarification",
      "clarification_field": "device",
      "facts": {
        "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
        "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
        "date": {
          "mode": "exact",
          "start_date": "2026-09-28",
          "end_date": null
        },
        "time": {
          "mode": "nearest",
          "start_time": "12:00",
          "end_time": null,
          "start_time_ambiguity": "none",
          "end_time_ambiguity": "none"
        },
        "package_usage": "unspecified",
        "exact_time_requested": false,
        "service_requires_laser_device": true,
        "compound_write_group": "compound:0,1",
        "compound_visit_group": "compound:0,1",
        "compound_visit_grouped": true,
        "compound_visit_preflight_resolved": true,
        "compound_visit_missing_device": true
      }
    }
  ],
  "verified_device_options": [
    "Prime Lase",
    "Candela Gentle"
  ],
  "expected_chain_with_prime_lase": [
    "2026-09-28T09:00:00+00:00",
    "2026-09-28T10:15:00+00:00"
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_04_shared_anchor_sequences_components

Category: grouped_atomicity
Purpose: One shared requested start must sequence components by canonical duration instead of overlapping them.
Review status: PENDING_MANUAL_REVIEW
Expected: First component starts at the anchor; second starts after the first ends; one visit group.

Customer 1: احجزيلي هيدرافيشل وتنظيف بشرة عميق يوم 2026-09-28 الساعة 10:00 مع مريم حسن، ورا بعض في نفس الزيارة
Tia: تم حجز هيدرافيشل يوم 28 سبتمبر الساعة 10:00، وبعده تنظيف بشرة عميق الساعة 11:15 مع مريم حسن، في نفس الزيارة.
Usage: in=8456 read=5430 write=2252 uncached=774 out=489 latency=12611ms

DB verification:
{
  "created": [
    {
      "id": "ead14b86-7a12-490a-bb6c-c6a7197f74eb",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "ead4328e-713d-5d89-8ce1-ac6e39a5e4b1",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "e84e11ea-6250-461e-9492-c260464c4483",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "ead4328e-713d-5d89-8ce1-ac6e39a5e4b1",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "expected_chain": [
    "2026-09-28T07:00:00+00:00",
    "2026-09-28T08:15:00+00:00"
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "e84e11ea-6250-461e-9492-c260464c4483",
        "ead14b86-7a12-490a-bb6c-c6a7197f74eb"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_05_reschedule_entire_group

Category: group_lifecycle
Purpose: A verified grouped visit should move as one logical visit.
Review status: PENDING_MANUAL_REVIEW
Expected: Both original members become rescheduled and exactly two sequential replacements form one logical visit.

Customer 1: غيري ميعاد الزيارة اللي فيها هيدرافيشل وتنظيف بشرة عميق كلها ليوم 2026-09-29 الساعة 10:00
Tia: تم تغيير ميعاد الزيارة بالكامل بنجاح ليوم 29 سبتمبر 2026 الساعة 10:00 صباحًا مع د. مريم حسن في Tia Clinic، وتشمل هيدرافيشل وتنظيف بشرة عميق. إجمالي الزيارة 3000 جنيه.
Usage: in=8137 read=5430 write=1939 uncached=768 out=475 latency=11371ms

DB verification:
{
  "original": [
    {
      "id": "bee57a2f-bc7e-4ea9-bfbe-2dd01545e5a7",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "445d8402-2fde-44ba-b109-9ad288733c85",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "old_after": [
    {
      "id": "bee57a2f-bc7e-4ea9-bfbe-2dd01545e5a7",
      "status": "rescheduled",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "445d8402-2fde-44ba-b109-9ad288733c85",
      "status": "rescheduled",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "replacements": [
    {
      "id": "caf5fb1b-cb61-4187-bee8-08a2e1de3efe",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-29T07:00:00+00:00",
      "end_at": "2026-09-29T08:00:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": "bee57a2f-bc7e-4ea9-bfbe-2dd01545e5a7"
    },
    {
      "id": "42d63c73-6213-4b27-8933-370f63734301",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-29T08:15:00+00:00",
      "end_at": "2026-09-29T09:15:00+00:00",
      "visit_group_id": "90431cbb-278b-4803-8258-7689e97b2b1e",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": "445d8402-2fde-44ba-b109-9ad288733c85"
    }
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "42d63c73-6213-4b27-8933-370f63734301",
        "caf5fb1b-cb61-4187-bee8-08a2e1de3efe"
      ],
      "removed": [],
      "changed": [
        "445d8402-2fde-44ba-b109-9ad288733c85",
        "bee57a2f-bc7e-4ea9-bfbe-2dd01545e5a7"
      ]
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_06_cancel_entire_standard_group

Category: group_lifecycle
Purpose: General visit cancellation must treat the grouped visit consistently.
Review status: PENDING_MANUAL_REVIEW
Expected: Cancel every intended group member exactly once, or fail closed consistently with no partial mutation.

Customer 1: الغِ الزيارة كلها يوم 2026-09-28 اللي فيها هيدرافيشل وتنظيف بشرة عميق
Tia: تم إلغاء الزيارة بالكامل يوم 28 سبتمبر 2026، واللي كانت بتشمل هيدرافيشل وتنظيف بشرة عميق.
Usage: in=7993 read=5430 write=1803 uncached=760 out=287 latency=6585ms

DB verification:
{
  "original": [
    {
      "id": "6345ba72-e6d0-4f1f-8d43-b904e4e8d2d7",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "57560c57-4235-4028-89fe-5981369bdfe6",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "0eb3a4b3-7c5b-4118-b568-1331e476bf7f",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "57560c57-4235-4028-89fe-5981369bdfe6",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "members_after": [
    {
      "id": "6345ba72-e6d0-4f1f-8d43-b904e4e8d2d7",
      "status": "cancelled",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "57560c57-4235-4028-89fe-5981369bdfe6",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "0eb3a4b3-7c5b-4118-b568-1331e476bf7f",
      "status": "cancelled",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "57560c57-4235-4028-89fe-5981369bdfe6",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "all_cancelled": true,
  "safe_handoff": false,
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": [
        "0eb3a4b3-7c5b-4118-b568-1331e476bf7f",
        "6345ba72-e6d0-4f1f-8d43-b904e4e8d2d7"
      ]
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_07_package_component_plus_standard_component

Category: mixed_package_standard
Purpose: Package entitlement must stay scoped to its eligible component inside one grouped visit.
Review status: PENDING_MANUAL_REVIEW
Expected: Hydrafacial uses its package; the second service remains standard; no payment or Pulse mutation.

Customer 1: احجزيلي هيدرافيشل من الباكدج وتنظيف بشرة عميق عادي يوم 2026-09-28 الساعة 10:00 مع مريم حسن، ورا بعض في نفس الزيارة
Tia: تم حجز هيدرافيشل من الباكدج يوم 28 سبتمبر 2026 الساعة 10:00، وتنظيف بشرة عميق عادي الساعة 11:15 مع مريم حسن، ورا بعض في نفس الزيارة.
Usage: in=8486 read=5430 write=2275 uncached=781 out=666 latency=17449ms

DB verification:
{
  "package_id": "840c41e1-7799-43b5-870e-93b1a53f0844",
  "created": [
    {
      "id": "413aa7cd-cfbe-40a1-b83c-7d21312df377",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "19687276-6132-500c-ba81-ce9171cbfc50",
      "billing_context": "package_prepaid",
      "patient_package_id": "840c41e1-7799-43b5-870e-93b1a53f0844",
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "045eb7ce-aa57-496c-9b6c-1dd8a881b7d6",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "19687276-6132-500c-ba81-ce9171cbfc50",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "package_usage_created": [
    "99d8edbe-3e8c-491e-9d40-0c9599ed6c86"
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "045eb7ce-aa57-496c-9b6c-1dd8a881b7d6",
        "413aa7cd-cfbe-40a1-b83c-7d21312df377"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": [
        "840c41e1-7799-43b5-870e-93b1a53f0844"
      ]
    },
    "package_usages": {
      "created": [
        "99d8edbe-3e8c-491e-9d40-0c9599ed6c86"
      ],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_08_financial_handoff_blocks_grouped_write

Category: ownership_boundary
Purpose: A Reception-owned ledger question combined with a grouped write must fail closed before partial booking.
Review status: PENDING_MANUAL_REVIEW
Expected: Handoff only; zero grouped appointment/package/payment/Pulse writes.

Customer 1: احجزيلي هيدرافيشل وتنظيف بشرة عميق في نفس الزيارة أقرب ميعاد، وبالمرة قولي أنا دفعت كام قبل كده
Tia: هحوّل طلب الحجز وسؤالك عن المدفوعات السابقة للاستقبال، وهيتواصلوا معاكي للتأكيد والمساعدة.
Usage: in=7494 read=5430 write=1294 uncached=770 out=523 latency=9872ms

DB verification:
{
  "business_delta_clean": true,
  "handoff": true,
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [
        "bda5e5de-294c-46fa-8e2f-10b2a24c822b"
      ],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": [
    {
      "status": "pending",
      "category": "payment",
      "priority": "normal",
      "source": "ai"
    }
  ]
}
Deterministic findings: none; manual review still required.

## b6_09_buy_package_and_book_same_service

Category: package_dependency
Purpose: Package purchase must precede and ground the dependent booking without inventing a payment.
Review status: PENDING_MANUAL_REVIEW
Expected: Package purchase and the exact dependent booking succeed atomically with one entitlement reservation, or the whole compound action rolls back. Never record payment implicitly.

Customer 1: اشتريلي باقة 4 جلسات هيدرافيشل واحجزيلي أول جلسة يوم 2026-09-28 الساعة 12:00 مع أحمد محمود
Tia: تم شراء باقة 4 جلسات هيدرافيشل، وتم حجز أول جلسة يوم 28 سبتمبر 2026 الساعة 12:00 مع أحمد محمود باستخدام الباقة الحالية. سعر الباقة 5000 جنيه.
Usage: in=7965 read=5430 write=1765 uncached=770 out=455 latency=10594ms

DB verification:
{
  "new_packages": [
    {
      "id": "2f406f33-1a47-4ae2-89be-4399bae30c05",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "name": "هيدرافيشل · 4 sessions",
      "status": "active",
      "remaining": null
    }
  ],
  "created": [
    {
      "id": "bf8ad11a-68be-4702-955a-9fc21d214648",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T09:00:00+00:00",
      "end_at": "2026-09-28T10:00:00+00:00",
      "visit_group_id": null,
      "billing_context": "package_prepaid",
      "patient_package_id": "2f406f33-1a47-4ae2-89be-4399bae30c05",
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "package_usage_created": [
    "42598e02-e9c5-4af6-b6a7-95a9566e153e"
  ],
  "payments_created": [],
  "operation_order": [
    "buy_package",
    "book"
  ],
  "linked_booking": true,
  "all_success": true,
  "fully_rolled_back": false,
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "bf8ad11a-68be-4702-955a-9fc21d214648"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [
        "2f406f33-1a47-4ae2-89be-4399bae30c05"
      ],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [
        "42598e02-e9c5-4af6-b6a7-95a9566e153e"
      ],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_10_buy_package_a_book_a_and_b

Category: package_dependency
Purpose: A package purchase plus two-service visit must keep entitlement scoped to A and remain atomic on failure.
Review status: PENDING_MANUAL_REVIEW
Expected: Either all dependent writes succeed with package only on A, or the grouped write rolls back completely.

Customer 1: اشتريلي باقة 4 جلسات هيدرافيشل واحجزيلي هيدرافيشل وتنظيف بشرة عميق في نفس الزيارة يوم 2026-09-28 الساعة 10:00 مع مريم حسن، ورا بعض
Tia: تم شراء باقة 4 جلسات هيدرافيشل بسعر 5000 جنيه، وتم حجز نفس الزيارة يوم 28 سبتمبر 2026 مع مريم حسن: هيدرافيشل الساعة 10:00 باستخدام الباقة، وتنظيف بشرة عميق الساعة 11:15 ورا بعض.
Usage: in=8933 read=5430 write=2716 uncached=787 out=777 latency=19372ms

DB verification:
{
  "new_packages": [
    {
      "id": "f77129c2-0614-4da0-bfc3-9a8b35b55120",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "name": "هيدرافيشل · 4 sessions",
      "status": "active",
      "remaining": null
    }
  ],
  "created": [
    {
      "id": "763aa44d-5b22-45db-85ec-27a476c156d7",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "b51a1e17-5e9c-5674-a004-5a3f55962f77",
      "billing_context": "package_prepaid",
      "patient_package_id": "f77129c2-0614-4da0-bfc3-9a8b35b55120",
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "546b1d70-63a7-4a43-b8c0-ca67f740127e",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "b51a1e17-5e9c-5674-a004-5a3f55962f77",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "package_usage_created": [
    "3a6ef04e-bf1b-4f3d-9179-87cbb16a976e"
  ],
  "all_success": true,
  "fully_rolled_back": false,
  "expected_chain": [
    "2026-09-28T07:00:00+00:00",
    "2026-09-28T08:15:00+00:00"
  ],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "546b1d70-63a7-4a43-b8c0-ca67f740127e",
        "763aa44d-5b22-45db-85ec-27a476c156d7"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [
        "f77129c2-0614-4da0-bfc3-9a8b35b55120"
      ],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [
        "3a6ef04e-bf1b-4f3d-9179-87cbb16a976e"
      ],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_11_replace_one_service_before_commit

Category: compound_correction
Purpose: Replacing one component before commit must remove the old component without disturbing the other service.
Review status: PENDING_MANUAL_REVIEW
Expected: Final state contains A+C only, or remains safely pending with zero writes; never ghost-book B.

Customer 1: احجزيلي هيدرافيشل وليزر إزالة الشعر - إبط في نفس الزيارة يوم 2026-09-28 الساعة 10:00 مع مريم حسن، ورا بعض
Tia: تمام، عشان أحجز الزيارتين ورا بعض مع مريم حسن يوم 28 سبتمبر، اختاري جهاز الليزر: **Candela Gentle** أو **Prime Lase**. المتاح لليزر من الساعة 11:00 صباحًا لحد 5:00 مساءً، وسعره 550 أو 650 جنيه حسب الجهاز.
Usage: in=7950 read=5430 write=1746 uncached=774 out=684 latency=9967ms

Customer 2: لا، بدل ليزر إزالة الشعر - إبط خليها تنظيف بشرة عميق وكملي الحجز
Tia: تمام، اتبدّل الليزر لتنظيف بشرة عميق واتحجزت الزيارة مع مريم حسن يوم 28 سبتمبر: هيدرافيشل الساعة 10:00 صباحًا، وتنظيف بشرة عميق الساعة 11:15 صباحًا، ورا بعض.
Usage: in=12936 read=5430 write=2359 uncached=5147 out=887 latency=20271ms

DB verification:
{
  "mid_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "created": [
    {
      "id": "1be6df4b-1d12-4a44-b8ab-f0897d9f603a",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "b37b6c9b-765e-5869-85e4-66b9634e4c93",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "41dbfb1d-f0ee-412c-9713-3b28e3e005bf",
      "status": "confirmed",
      "service_id": "c8f3fdac-11fd-522a-83b9-322a87c97830",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T09:15:00+00:00",
      "visit_group_id": "b37b6c9b-765e-5869-85e4-66b9634e4c93",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "ghost_old_service": false,
  "anchor": "2026-09-28T07:00:00+00:00",
  "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "1be6df4b-1d12-4a44-b8ab-f0897d9f603a",
        "41dbfb1d-f0ee-412c-9713-3b28e3e005bf"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_12_change_device_for_one_component

Category: compound_correction
Purpose: Changing the laser device must revalidate only that component and preserve the standard component context.
Review status: PENDING_MANUAL_REVIEW
Expected: Prime Lase is used only for the laser component; no stale device slot or partial group.

Customer 1: احجزيلي هيدرافيشل وليزر إزالة الشعر - إبط في نفس الزيارة يوم 2026-09-28 مع مريم حسن، والليزر على Candela Gentle
Tia: متاحين في نفس الزيارة يوم 28 سبتمبر مع مريم حسن: هيدرافيشل الساعة 10:00 صباحًا بسعر 1800 جنيه، وليزر إزالة الشعر للإبط الساعة 11:15 صباحًا على جهاز Candela Gentle بسعر 650 جنيه. أأكد الحجز؟
Usage: in=8028 read=5430 write=1825 uncached=773 out=650 latency=14307ms

Customer 2: خلي الليزر على Prime Lase والساعة 10:00 وكملي الحجز
Tia: فيه مواعيد متاحة مؤكدة، لكن تفاصيل الفترة مش متاحة للعرض هنا.
Usage: in=9160 read=5430 write=2351 uncached=1379 out=677 latency=16261ms

DB verification:
{
  "mid_created": [],
  "created": [
    {
      "id": "65277a40-c856-4837-a45f-9b316292f4aa",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "3f170224-74e6-5479-acc9-d5e42c080579",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "9a4184cf-4b3e-493d-b771-90ccaa646a72",
      "status": "confirmed",
      "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T08:30:00+00:00",
      "visit_group_id": "3f170224-74e6-5479-acc9-d5e42c080579",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": "prime_lase",
      "laser_device_name": "Prime Lase",
      "rescheduled_from_appointment_id": null
    }
  ],
  "anchor": "2026-09-28T07:00:00+00:00",
  "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "65277a40-c856-4837-a45f-9b316292f4aa",
        "9a4184cf-4b3e-493d-b771-90ccaa646a72"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_13_side_price_query_preserves_compound

Category: compound_continuity
Purpose: A grounded price side query must not reset or duplicate the multi-component booking context.
Review status: PENDING_MANUAL_REVIEW
Expected: Price read is grounded and read-only; compound flow remains coherent and can continue without duplicate/partial writes.

Customer 1: احجزيلي هيدرافيشل وليزر إزالة الشعر - إبط في نفس الزيارة يوم 2026-09-28 مع مريم حسن، والليزر على Prime Lase
Tia: المتاح يوم 28 سبتمبر مع د. مريم حسن: هيدرافيشل الساعة 10:00 صباحًا، وليزر إزالة الشعر للإبط الساعة 11:15 على جهاز Prime Lase، وسعر الهيدرافيشل 1800 جنيه والليزر 550 جنيه. هل المواعيد دي مناسبة؟
Usage: in=8028 read=5430 write=1825 uncached=773 out=699 latency=14201ms

Customer 2: على فكرة سعر ليزر إزالة الشعر - إبط على Prime Lase كام؟
Tia: جلسة ليزر إزالة الشعر - إبط على Prime Lase سعرها 550 جنيه.
Usage: in=6805 read=5430 write=0 uncached=1375 out=269 latency=6246ms

Customer 3: تمام خليه Prime Lase والساعة 10:00 وكملي الحجز
Tia: تمام، الحجز اتأكد يوم 28 سبتمبر مع مريم حسن: هيدرافيشل الساعة 10:00 صباحًا، وليزر الإبط على جهاز Prime Lase الساعة 11:15 صباحًا.
Usage: in=9178 read=5430 write=2397 uncached=1351 out=780 latency=16717ms

DB verification:
{
  "mid_created": [],
  "final_created": [
    {
      "id": "a2ed3d9e-991f-459a-a84c-d01923168a04",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": "4354b395-0212-5993-b956-7e7bedcd613b",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    },
    {
      "id": "6dbf9880-9b41-4667-9eb3-63cc3514294c",
      "status": "confirmed",
      "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T08:15:00+00:00",
      "end_at": "2026-09-28T08:30:00+00:00",
      "visit_group_id": "4354b395-0212-5993-b956-7e7bedcd613b",
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": "prime_lase",
      "laser_device_name": "Prime Lase",
      "rescheduled_from_appointment_id": null
    }
  ],
  "price_turn_reads": [
    "service_catalog"
  ],
  "anchor": "2026-09-28T07:00:00+00:00",
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "6dbf9880-9b41-4667-9eb3-63cc3514294c",
        "a2ed3d9e-991f-459a-a84c-d01923168a04"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_14_remove_one_component

Category: compound_correction
Purpose: Removing one pending component must collapse the grouped intent cleanly without a ghost booking.
Review status: PENDING_MANUAL_REVIEW
Expected: Exactly the retained service is booked, or the flow stays safely pending; removed service is never written.

Customer 1: احجزيلي هيدرافيشل وليزر إزالة الشعر - إبط في نفس الزيارة يوم 2026-09-28 مع مريم حسن، والليزر على Prime Lase
Tia: المتاح يوم 28 سبتمبر مع د. مريم حسن في نفس الزيارة: هيدرافيشل الساعة 10:00 صباحًا بسعر 1800 جنيه، وليزر إزالة الشعر للإبط الساعة 11:15 صباحًا على جهاز Prime Lase بسعر 550 جنيه. تحبي أأكد الحجز؟
Usage: in=8028 read=5430 write=1825 uncached=773 out=670 latency=13478ms

Customer 2: سيبي ليزر إزالة الشعر - إبط وخلي هيدرافيشل بس الساعة 10:00 وكملي
Tia: فيه مواعيد متاحة مؤكدة، لكن تفاصيل الفترة مش متاحة للعرض هنا.
Usage: in=8487 read=5430 write=1671 uncached=1386 out=497 latency=12901ms

DB verification:
{
  "created": [
    {
      "id": "1a39d288-cd2c-4b72-aa2d-876cd89b346b",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T07:00:00+00:00",
      "end_at": "2026-09-28T08:00:00+00:00",
      "visit_group_id": null,
      "billing_context": "standard",
      "patient_package_id": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "removed_service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
  "anchor": "2026-09-28T07:00:00+00:00",
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [
        "1a39d288-cd2c-4b72-aa2d-876cd89b346b"
      ],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_15_sequence_crosses_resource_boundary

Category: grouped_atomicity
Purpose: A first component that fits must not commit when the complete sequential visit cannot fit.
Review status: PENDING_MANUAL_REVIEW
Expected: Zero partial grouped write when the complete sequence cannot fit the canonical resource window.

Customer 1: هل هيدرافيشل وتنظيف بشرة عميق متاحين في نفس الزيارة يوم 2026-09-28 الساعة 17:00 مع مريم حسن؟
Tia: ممكن تحددي الخدمة المطلوبة الأول: هيدرافيشل ولا تنظيف بشرة عميق؟
Usage: in=7551 read=5430 write=1350 uncached=771 out=477 latency=7634ms

Customer 2: تمام احجزيهم في المعاد ده
Tia: المتاح يوم 29 سبتمبر مع مريم حسن: هيدرافيشل الساعة 10:00 صباحًا، وتنظيف بشرة عميق الساعة 11:15 صباحًا. تحبي أحجز الخدمتين في الزيارتين دول؟
Usage: in=8095 read=5430 write=1855 uncached=810 out=790 latency=17186ms

DB verification:
{
  "anchor": "2026-09-28T14:00:00+00:00",
  "created": [],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## b6_16_canonical_state_changes_before_compound_commit

Category: stale_state_boundary
Purpose: A canonical resource change between turns must force fresh verification and suppress the entire stale grouped write.
Review status: PENDING_MANUAL_REVIEW
Expected: Fresh availability validation detects the conflict and creates zero grouped components.

Customer 1: هل هيدرافيشل وتنظيف بشرة عميق متاحين في نفس الزيارة يوم 2026-09-28 الساعة 10:00 مع مريم حسن؟
Tia: أيوه، الاتنين متاحين يوم 28 سبتمبر الساعة 10:00 مع مريم حسن: الهيدرافيشل بـ1800 جنيه وتنظيف البشرة العميق بـ1200 جنيه.
Usage: in=7853 read=5430 write=1652 uncached=771 out=493 latency=9278ms

Customer 2: تمام احجزي الزيارة دي
Tia: تمام، بس المواعيد المتاحة مش في نفس الوقت: الهيدرافيشل الساعة 12:15، وتنظيف البشرة العميق الساعة 13:30 يوم 28 سبتمبر مع مريم حسن. تحبي أحجز الخدمتين ورا بعض؟
Usage: in=8318 read=5430 write=1875 uncached=1013 out=743 latency=14112ms

DB verification:
{
  "competing_appointment_id": "681adb10-7285-4af1-8378-8c3004f6e786",
  "blocked_second_start": "2026-09-28T08:15:00+00:00",
  "confirm_reads": [
    "availability"
  ],
  "created": [],
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "db_delta": {
    "appointments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "packages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "package_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_packs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "payments": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_usages": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_settlements": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "handoffs": {
      "created": [],
      "removed": [],
      "changed": []
    },
    "pulse_balance_delta": {}
  },
  "handoffs": []
}
Deterministic findings: none; manual review still required.

## Batch summary

{
  "scenarios_run": 16,
  "pending_manual_review": 16,
  "infrastructure_failures": 0,
  "deterministic_P0": 0,
  "deterministic_P1": 0,
  "deterministic_P2": 0,
  "tokens": {
    "input_tokens": 200486,
    "output_tokens": 14438,
    "cached_tokens": 130320,
    "cache_write_tokens": 44315,
    "uncached_input_tokens": 25851,
    "total_tokens": 214924,
    "average_tokens_per_conversation": 13432.75,
    "median_tokens_per_conversation": 9431.0,
    "max_tokens": 25759,
    "max_scenario": "b6_13_side_price_query_preserves_compound",
    "min_tokens": 8017,
    "min_scenario": "b6_08_financial_handoff_blocks_grouped_write"
  },
  "total_turns": 24,
  "total_llm_calls": 47,
  "interpreter_calls": 24,
  "responder_calls": 23,
  "stage_metrics": {
    "interpreter": {
      "calls": 24,
      "input_tokens": 156102,
      "cached_read_tokens": 130320,
      "cache_write_tokens": 0,
      "uncached_tokens": 25782,
      "output_tokens": 11251,
      "latency_ms": 104937,
      "retries": 0,
      "fallback_calls": 0
    },
    "responder": {
      "calls": 23,
      "input_tokens": 44384,
      "cached_read_tokens": 0,
      "cache_write_tokens": 44315,
      "uncached_tokens": 69,
      "output_tokens": 3187,
      "latency_ms": 56911,
      "retries": 0,
      "fallback_calls": 0
    },
    "all": {
      "calls": 47,
      "input_tokens": 200486,
      "cached_read_tokens": 130320,
      "cache_write_tokens": 44315,
      "uncached_tokens": 25851,
      "output_tokens": 14438,
      "latency_ms": 161848,
      "retries": 0,
      "fallback_calls": 0
    },
    "turn_latency_ms": 310462
  },
  "atomicity_counters": {
    "partial_grouped_writes": 0,
    "wrong_group_membership": 0,
    "duplicate_grouped_visits": 0,
    "wrong_component_service": 0,
    "wrong_component_doctor_device": 0,
    "wrong_package_linkage": 0,
    "wrong_entitlement_mutation": 0,
    "stale_grouped_writes": 0
  },
  "global_safety_counters": {
    "cross_patient_reads": 0,
    "cross_patient_writes": 0,
    "wrong_patient_writes": 0,
    "financial_boundary_violations": 0,
    "human_ownership_writes": 0,
    "invented_entity_writes": 0
  },
  "cost": {
    "actual_usd": 0.03618095,
    "without_explicit_cache_usd": 0.0574228,
    "saving_usd": 0.02124185,
    "saving_percent": 36.99
  },
  "stopped_for_deterministic_p0": false
}