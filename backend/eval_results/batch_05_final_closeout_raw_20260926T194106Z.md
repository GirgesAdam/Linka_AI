# Tia Agent Evaluation — Batch 05 Raw Baseline

- Batch runtime base SHA: 2c060d51e399d82588671d16cbbf2f0b2916b9b6
- Harness SHA: 444d9091bff17c92c47b7a2000a875a0fd8680a9
- Scenario version: batch5-v1
- Model: gpt-5.6-luna
- Reasoning: low
- Scenarios executed: 15

Raw deterministic findings are guards; final verdict requires manual DB/write/response review.

## b5_01_same_name_different_patients

Category: patient_identity
Purpose: Ambiguous same-name third-party patients must never be guessed from customer text.
Review status: PENDING_MANUAL_REVIEW
Expected: No cross-patient read-derived mutation; clarify or stay scoped to the verified current customer.

Customer 1: غيري ميعاد محمد أحمد
Tia: ممكن تحدديلي التاريخ الجديد المطلوب لمعاد محمد أحمد؟
Usage: in=7372 read=5353 write=1279 uncached=740 out=311 latency=9909ms

DB verification:
{
  "ambiguous_patient_ids": [
    "c739a680-e1b1-4cee-9833-cb477694a8d6",
    "9ffd749c-5af1-4bd3-a862-174ca8df5439"
  ],
  "ambiguous_appointment_ids": [
    "9265221f-dedd-4da4-adad-e0780c6fece4",
    "a87c7c25-937f-4663-93ea-6e12f68a807d"
  ],
  "other_before": {
    "c739a680-e1b1-4cee-9833-cb477694a8d6": [
      {
        "id": "9265221f-dedd-4da4-adad-e0780c6fece4",
        "status": "confirmed",
        "start_at": "2026-09-29T07:00:00+00:00",
        "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
        "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
        "laser_device_key": null,
        "rescheduled_from_appointment_id": null
      }
    ],
    "9ffd749c-5af1-4bd3-a862-174ca8df5439": [
      {
        "id": "a87c7c25-937f-4663-93ea-6e12f68a807d",
        "status": "confirmed",
        "start_at": "2026-09-29T07:00:00+00:00",
        "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
        "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
        "laser_device_key": null,
        "rescheduled_from_appointment_id": null
      }
    ]
  },
  "other_after": {
    "c739a680-e1b1-4cee-9833-cb477694a8d6": [
      {
        "id": "9265221f-dedd-4da4-adad-e0780c6fece4",
        "status": "confirmed",
        "start_at": "2026-09-29T07:00:00+00:00",
        "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
        "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
        "laser_device_key": null,
        "rescheduled_from_appointment_id": null
      }
    ],
    "9ffd749c-5af1-4bd3-a862-174ca8df5439": [
      {
        "id": "a87c7c25-937f-4663-93ea-6e12f68a807d",
        "status": "confirmed",
        "start_at": "2026-09-29T07:00:00+00:00",
        "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
        "doctor_id": "d18b8596-9342-57d9-a03d-6ef674a6f5e0",
        "laser_device_key": null,
        "rescheduled_from_appointment_id": null
      }
    ]
  },
  "availability_dates": [
    "2026-09-29",
    "2026-09-29"
  ],
  "cross_patient_mutation": false,
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

## b5_02_phone_beats_ambiguous_name

Category: patient_identity
Purpose: Verified current-customer identity must beat an ambiguous duplicated name.
Review status: PENDING_MANUAL_REVIEW
Expected: Only the verified current customer's appointment is rescheduled; the same-name patient is untouched.

Customer 1: أنا محمد أحمد، غيري ميعادي وخليه يوم 2026-10-01 الساعة 14:00
Tia: تمام يا محمد، تم تغيير ميعادك لهيدرافيشل ليوم 1 أكتوبر 2026 الساعة 2:00 مساءً مع عمر خليل في Tia Clinic. الميعاد غير مدفوع حاليًا، والسعر 1800 جنيه.
Usage: in=8003 read=5353 write=1892 uncached=758 out=301 latency=9572ms

DB verification:
{
  "current_patient_id": "576421f5-b895-587a-a3c3-196f183ccda2",
  "current_phone": "+201010000012",
  "other_patient_id": "1d2fcb03-3bb9-4edd-a2da-6c79b32c6468",
  "other_appointment_id": "8a7678c2-7864-435b-9f2f-74f5af1528ea",
  "other_unchanged": true,
  "replacements": [
    {
      "id": "d14a8597-3a32-4996-a417-fb94d29703e7",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-10-01T11:00:00+00:00",
      "billing_context": "standard",
      "payment_status": "unpaid",
      "amount_paid_minor": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "patient_package_id": null,
      "rescheduled_from_appointment_id": "992796ab-83ef-4137-8f75-85d474b6ecde"
    }
  ],
  "db_delta": {
    "appointments": {
      "created": [
        "d14a8597-3a32-4996-a417-fb94d29703e7"
      ],
      "removed": [],
      "changed": [
        "992796ab-83ef-4137-8f75-85d474b6ecde"
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

## b5_03_historical_name_not_current_identity

Category: patient_identity
Purpose: A stale historical patient name must not override the verified current customer.
Review status: PENDING_MANUAL_REVIEW
Expected: Canonical current identity wins; no cross-patient read/write occurs.

Customer 1: أنا سارة علي، خليه يوم 2026-10-01 الساعة 14:00
Tia: تم تعديل ميعادك لهيدرافيشل ليوم 1 أكتوبر 2026 الساعة 2:00 ظهرًا مع د. عمر خليل في Tia Clinic.
Usage: in=8055 read=5353 write=1918 uncached=784 out=433 latency=9645ms

DB verification:
{
  "current_patient_id": "576421f5-b895-587a-a3c3-196f183ccda2",
  "historical_name": "محمود حسن",
  "other_patient_id": "823ea517-e4ba-4ac4-81f6-226376751489",
  "other_appointment_id": "4879bc16-0fba-446a-8908-ad834c10bbef",
  "other_unchanged": true,
  "replacements": [
    {
      "id": "a9a0623d-f193-4f23-8861-8e37c0f7b4cc",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-10-01T11:00:00+00:00",
      "billing_context": "standard",
      "payment_status": "unpaid",
      "amount_paid_minor": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "patient_package_id": null,
      "rescheduled_from_appointment_id": "f8d1509a-19dd-499c-a9f1-c1f7c74e665d"
    }
  ],
  "db_delta": {
    "appointments": {
      "created": [
        "a9a0623d-f193-4f23-8861-8e37c0f7b4cc"
      ],
      "removed": [],
      "changed": [
        "f8d1509a-19dd-499c-a9f1-c1f7c74e665d"
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

## b5_04_slot_becomes_unavailable

Category: changing_scheduling_truth
Purpose: A slot verified earlier must not remain writable after canonical availability changes.
Review status: PENDING_MANUAL_REVIEW
Expected: No double booking; write-time/canonical validation wins and the stale slot is reported unavailable.

Customer 1: هل هيدرافيشل يوم 2026-09-29 الساعة 10:00 مع عمر خليل متاح؟
Tia: أيوه، هيدرافيشل متاح يوم 29 سبتمبر 2026 الساعة 10:00 صباحًا مع عمر خليل، وسعر الجلسة 1800 جنيه مصري.
Usage: in=7555 read=5353 write=1443 uncached=759 out=280 latency=6122ms

Customer 2: تمام احجزيه
Tia: للأسف، هيدرافيشل مع عمر خليل يوم 29 سبتمبر الساعة 10:00 مش متاح، فمش هقدر أحجزه في الوقت ده. ابعتيلي وقت بديل مناسب.
Usage: in=7785 read=5353 write=1432 uncached=1000 out=434 latency=9217ms

DB verification:
{
  "requested_start_at": "2026-09-29T07:00:00+00:00",
  "competing_appointment_id": "2e505604-5d51-4aa3-934a-4ecd75476629",
  "created_for_customer": [],
  "booking_turn_reads": [
    "availability"
  ],
  "booking_write_result": null,
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

## b5_05_doctor_schedule_changes_between_turns

Category: changing_scheduling_truth
Purpose: Doctor schedule changes after an earlier availability answer must invalidate that answer.
Review status: PENDING_MANUAL_REVIEW
Expected: Fresh canonical doctor availability is checked before write; no stale-slot booking occurs.

Customer 1: هل هيدرافيشل يوم 2026-09-28 الساعة 12:00 مع أحمد محمود متاح؟
Tia: أيوه، هيدرافيشل متاح يوم 28 سبتمبر الساعة 12:00 ظهرًا مع أحمد محمود، والسعر 1800 جنيه.
Usage: in=7553 read=5353 write=1442 uncached=758 out=319 latency=6546ms

Customer 2: تمام احجزيه
Tia: الساعة 12:00 يوم 28 سبتمبر مش متاحة لهيدرافيشل مع أحمد محمود، ومفيش مواعيد بديلة متوفرة حاليًا.
Usage: in=7771 read=5353 write=1422 uncached=996 out=380 latency=7135ms

DB verification:
{
  "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
  "requested_start_at": "2026-09-28T09:00:00+00:00",
  "working_hours_removed": 1,
  "booking_turn_reads": [
    "availability"
  ],
  "created_for_customer": [],
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

## b5_06_cancelled_externally_before_reschedule

Category: changing_scheduling_truth
Purpose: Externally cancelled appointments must not be resurrected by stale conversational reschedule state.
Review status: PENDING_MANUAL_REVIEW
Expected: Canonical cancelled state wins; no reschedule/revival write occurs.

Customer 1: ممكن أغير ميعادي؟
Tia: أيوه طبعًا. تحبي تغيّريه ليوم إيه؟ ميعادك الحالي يوم ٢٩ سبتمبر الساعة ١٠ صباحًا مع د. عمر خليل.
Usage: in=7652 read=5353 write=1557 uncached=742 out=257 latency=6054ms

Customer 2: خليه يوم 2026-10-01 الساعة 14:00
Tia: ميعاد 1 أكتوبر الساعة 2 ظهرًا متاح للهيدرافيشل مع د. عمر خليل. تحبي تحجزيه باستخدام باكدج موجودة ولا من غير باكدج؟
Usage: in=7742 read=5353 write=1517 uncached=872 out=621 latency=12184ms

DB verification:
{
  "source_id": "1248d35d-641a-4efa-b0c7-ed45614b1332",
  "external_status_before_customer_turn": "cancelled",
  "appointment_after": {
    "id": "1248d35d-641a-4efa-b0c7-ed45614b1332",
    "status": "cancelled",
    "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
    "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
    "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
    "start_at": "2026-09-29T07:00:00+00:00",
    "billing_context": "standard",
    "payment_status": "unpaid",
    "amount_paid_minor": null,
    "laser_device_key": null,
    "laser_device_name": null,
    "patient_package_id": null,
    "rescheduled_from_appointment_id": null
  },
  "replacements": [],
  "second_turn_reads": [
    "appointments",
    "availability"
  ],
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

## b5_07_same_day_current_time_boundary

Category: changing_scheduling_truth
Purpose: Same-day booking must follow the current canonical notice rule, not a stale one-hour assumption.
Review status: PENDING_MANUAL_REVIEW
Expected: The earliest canonical same-day slot is bookable exactly when current business rules allow it.

Customer 1: احجزيلي هيدرافيشل النهارده 2026-09-26 الساعة 10:00 مع هالة مصطفى
Tia: تم حجز جلسة هيدرافيشل ليكي النهارده 26 سبتمبر 2026 الساعة 10:00 مع هالة مصطفى، بسعر 2000 جنيه.
Usage: in=7667 read=6908 write=0 uncached=759 out=351 latency=8425ms

DB verification:
{
  "local_now": "2026-09-26T09:00:00+03:00",
  "canonical_minimum_notice_minutes": 60,
  "requested_notice_minutes": 60,
  "requested_start_at": "2026-09-26T07:00:00+00:00",
  "created": [
    {
      "id": "6b38b446-fcdd-4c77-8b6e-fa0762fd2499",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "e4dcaadf-822d-5f5a-9df7-6207243e9854",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-26T07:00:00+00:00",
      "billing_context": "standard",
      "payment_status": "unknown",
      "amount_paid_minor": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "patient_package_id": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "db_delta": {
    "appointments": {
      "created": [
        "6b38b446-fcdd-4c77-8b6e-fa0762fd2499"
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

## b5_08_doctor_does_not_offer_service

Category: compatibility
Purpose: An explicit doctor who does not offer the requested service must never be silently substituted into a booking.
Review status: PENDING_MANUAL_REVIEW
Expected: No incompatible booking; give a grounded correction or valid alternatives.

Customer 1: احجزيلي PRP للبشرة يوم 2026-10-02 الساعة 14:00 مع أحمد محمود
Tia: الدكتور أحمد محمود مش متوافق مع خدمة PRP للبشرة. الدكاترة المتوافقين مع الخدمة: مها. اختاري دكتور منهم عشان أكمل الحجز.
Usage: in=6108 read=5353 write=0 uncached=755 out=217 latency=5727ms

DB verification:
{
  "service_id": "abf2f14d-713d-543d-ad14-79837d909b5c",
  "incompatible_doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
  "requested_date": "2026-10-02",
  "requested_time": "14:00",
  "created": [],
  "verified_reads": [
    "availability"
  ],
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

## b5_09_laser_requires_device

Category: compatibility
Purpose: A device-required laser service must resolve a compatible device canonically or clarify.
Review status: PENDING_MANUAL_REVIEW
Expected: No invented device; any booking uses a verified compatible device, otherwise the Agent clarifies.

Customer 1: احجزيلي ليزر إزالة الشعر - إبط يوم 2026-09-28 الساعة 12:00 مع أحمد محمود
Tia: متاح يوم 28 سبتمبر الساعة 12:00 مع د. أحمد محمود، بس محتاجة تختاري الجهاز: Candela Gentle بسعر 550 جنيه أو Prime Lase بسعر 650 جنيه.
Usage: in=7670 read=5353 write=1556 uncached=761 out=283 latency=6422ms

DB verification:
{
  "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
  "valid_device_keys": [
    "candela_gentle",
    "prime_lase"
  ],
  "created": [],
  "verified_reads": [
    "availability"
  ],
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

## b5_10_explicit_incompatible_device

Category: compatibility
Purpose: An explicitly requested device that is not canonical for the service must not be silently retained or substituted.
Review status: PENDING_MANUAL_REVIEW
Expected: No booking with the incompatible explicit service/device pair; correction or clarification only.

Customer 1: احجزيلي ليزر إزالة الشعر - إبط يوم 2026-09-28 الساعة 12:00 مع أحمد محمود على Candela Gentle
Tia: الجهاز Candela Gentle مش متوافق مع خدمة ليزر إزالة الشعر - إبط. الأجهزة المتوافقين مع الخدمة: Prime Lase. اختاري جهاز منهم عشان أكمل الحجز.
Usage: in=6115 read=5353 write=0 uncached=762 out=241 latency=6920ms

DB verification:
{
  "service_id": "ff83c8db-6e9a-5b5b-9e5b-b8d89692d28e",
  "explicit_device": "candela_gentle",
  "valid_device_keys_after_fixture_change": [
    "prime_lase"
  ],
  "created": [],
  "verified_reads": [
    "availability"
  ],
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

## b5_11_doctor_change_invalidates_availability

Category: compatibility
Purpose: Changing doctors must invalidate availability shown for the previous doctor.
Review status: PENDING_MANUAL_REVIEW
Expected: Doctor B availability is freshly verified; no Doctor A slot is reused.

Customer 1: عايزة أحجز هيدرافيشل يوم 2026-09-28 مع أحمد محمود، المواعيد إيه؟
Tia: متاح هيدرافيشل مع د. أحمد محمود يوم 28 سبتمبر من 12:00 ظهرًا لـ5:00 مساءً، ومن 6:15 لـ9:00 مساءً. السعر 1800 جنيه.
Usage: in=7597 read=5353 write=1483 uncached=761 out=310 latency=6752ms

Customer 2: لا خليها مع عمر خليل
Tia: تمام، المواعيد المتاحة للهيدرافيشل مع عمر خليل يوم 28 سبتمبر من 2:00 لـ3:30 ظهرًا، ومن 5:15 لـ9:00 مساءً. السعر 1800 جنيه.
Usage: in=7881 read=5353 write=1547 uncached=981 out=304 latency=7680ms

DB verification:
{
  "doctor_a_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
  "doctor_b_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
  "date": "2026-09-28",
  "doctor_a_unique_start": "2026-09-28T09:00:00+00:00",
  "doctor_b_slot_count": 23,
  "changed_turn_reads": [
    "availability"
  ],
  "created": [],
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

## b5_12_duplicate_booking_after_external_change

Category: stale_duplicate_safety
Purpose: A repeated confirmation after an external state change must consult canonical state instead of blindly acknowledging the old action.
Review status: PENDING_MANUAL_REVIEW
Expected: Recent-action context never masks the external canonical change; at most one active booking remains.

Customer 1: احجزيلي هيدرافيشل يوم 2026-09-29 الساعة 10:00 مع عمر خليل
Tia: تم حجز جلسة هيدرافيشل يوم 29 سبتمبر 2026 الساعة 10:00 مع عمر خليل، والسعر 1800 جنيه.
Usage: in=7670 read=5353 write=1558 uncached=759 out=320 latency=7149ms

Customer 2: اه تمام احجزيه كده
Tia: فيه مواعيد متاحة مؤكدة، لكن تفاصيل الفترة مش متاحة للعرض هنا.
Usage: in=7887 read=5353 write=1622 uncached=912 out=450 latency=9307ms

DB verification:
{
  "externally_cancelled_appointment_id": "01460f70-e9db-42fa-9898-3647a0cc79eb",
  "repeat_verified_reads": [
    "appointments",
    "availability"
  ],
  "repeat_write_attempted": true,
  "active_appointments_after": [
    {
      "id": "80fefa39-857d-4d06-82f0-5589fecee343",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-29T07:00:00+00:00",
      "billing_context": "standard",
      "payment_status": "unknown",
      "amount_paid_minor": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "patient_package_id": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "db_delta": {
    "appointments": {
      "created": [
        "80fefa39-857d-4d06-82f0-5589fecee343"
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

## b5_13_repeat_cancel_after_external_change

Category: stale_duplicate_safety
Purpose: Repeating cancellation after canonical state already changed must be non-destructive.
Review status: PENDING_MANUAL_REVIEW
Expected: No duplicate destructive cancellation; canonical cancelled state is respected.

Customer 1: الغيه
Tia: مش لاقية أي ميعاد هيدرافيشل مسجّل يوم 2026-09-29. ممكن تتأكدي من التاريخ أو تبعتيلي تاريخ الميعاد الصحيح؟
Usage: in=7471 read=5353 write=1333 uncached=785 out=410 latency=6641ms

DB verification:
{
  "appointment_id": "5c150397-1ee0-44dd-8cec-47b5c01d8319",
  "appointment_after": {
    "id": "5c150397-1ee0-44dd-8cec-47b5c01d8319",
    "status": "cancelled",
    "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
    "doctor_id": "6dd310a2-9f33-568f-b2e0-dd5db0e476c6",
    "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
    "start_at": "2026-09-29T07:00:00+00:00",
    "billing_context": "standard",
    "payment_status": "unpaid",
    "amount_paid_minor": null,
    "laser_device_key": null,
    "laser_device_name": null,
    "patient_package_id": null,
    "rescheduled_from_appointment_id": null
  },
  "verified_reads": [
    "appointments"
  ],
  "write_attempted": false,
  "write_result": null,
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

## b5_14_availability_then_competing_laser_booking

Category: stale_duplicate_safety
Purpose: A competing device booking after availability read must defeat the stale write safely.
Review status: PENDING_MANUAL_REVIEW
Expected: No device double-booking and no false successful write after the competing booking.

Customer 1: هل ليزر إزالة الشعر - إبط يوم 2026-09-28 الساعة 12:00 مع أحمد محمود على Candela Gentle متاح؟
Tia: أيوه، متاح يوم 28 سبتمبر 2026 الساعة 12:00 مع أحمد محمود على جهاز Candela Gentle. السعر 650 جنيه.
Usage: in=7577 read=5353 write=1459 uncached=765 out=291 latency=5975ms

Customer 2: تمام احجزيه
Tia: الميعاد ده الساعة 12:00 يوم 28 سبتمبر مش متاح، فمش هقدر أحجزه.
Usage: in=7836 read=5353 write=1432 uncached=1051 out=340 latency=7570ms

DB verification:
{
  "device_key": "candela_gentle",
  "requested_start_at": "2026-09-28T09:00:00+00:00",
  "competing_appointment_id": "c4d5e323-20ef-4c51-bb66-41a0377c8fa3",
  "confirm_reads": [
    "availability"
  ],
  "confirm_write_attempted": false,
  "confirm_write_result": null,
  "created_for_customer": [],
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

## b5_15_two_rapid_customer_turns

Category: stale_duplicate_safety
Purpose: Back-to-back booking then correction must converge to the latest legitimate intent without duplicate active appointments.
Review status: PENDING_MANUAL_REVIEW
Expected: Exactly one active appointment remains at the corrected time; no out-of-order stale write survives.

Customer 1: احجزيلي هيدرافيشل يوم 2026-09-28 الساعة 12:00 مع أحمد محمود
Tia: تم حجز جلسة هيدرافيشل يوم 28 سبتمبر 2026 الساعة 12:00 مع أحمد محمود. السعر 1800 جنيه.
Usage: in=7668 read=5353 write=1557 uncached=758 out=341 latency=7722ms

Customer 2: لا خليها الساعة 21:00
Tia: تمام، تقصدي يوم 28 سبتمبر الساعة 21:00؟
Usage: in=7964 read=5353 write=1702 uncached=909 out=560 latency=7497ms

DB verification:
{
  "conversation_id": "1884b038-363c-4a38-a575-cade677fa516",
  "first_requested_start": "2026-09-28T09:00:00+00:00",
  "second_requested_start": "2026-09-28T18:00:00+00:00",
  "turn_order": [
    1,
    2
  ],
  "active_appointments_after": [
    {
      "id": "adfbb158-9530-4ea9-8e45-77026083ae30",
      "status": "confirmed",
      "service_id": "8bcbaab6-775a-5d39-8f41-ececf3b90616",
      "doctor_id": "99902579-0e39-5df6-8f01-633b1ea859ef",
      "branch_id": "dc6c1238-9c5c-5251-a449-835881592b8b",
      "start_at": "2026-09-28T09:00:00+00:00",
      "billing_context": "standard",
      "payment_status": "unknown",
      "amount_paid_minor": null,
      "laser_device_key": null,
      "laser_device_name": null,
      "patient_package_id": null,
      "rescheduled_from_appointment_id": null
    }
  ],
  "correction_reads": [
    "appointments"
  ],
  "correction_write_result": null,
  "db_delta": {
    "appointments": {
      "created": [
        "adfbb158-9530-4ea9-8e45-77026083ae30"
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
Deterministic findings:
- P1: Rapid successive turns left a duplicate or stale active booking —

## Batch summary

{
  "scenarios_run": 15,
  "pending_manual_review": 15,
  "infrastructure_failures": 0,
  "deterministic_P0": 0,
  "deterministic_P1": 1,
  "deterministic_P2": 0,
  "tokens": {
    "input_tokens": 166599,
    "output_tokens": 7754,
    "cached_tokens": 119321,
    "cache_write_tokens": 29151,
    "uncached_input_tokens": 18127,
    "total_tokens": 174353,
    "average_tokens_per_conversation": 11623.53,
    "median_tokens_per_conversation": 8488,
    "max_tokens": 16533,
    "max_scenario": "b5_15_two_rapid_customer_turns",
    "min_tokens": 6325,
    "min_scenario": "b5_08_doctor_does_not_offer_service"
  },
  "total_turns": 22,
  "total_llm_calls": 42,
  "interpreter_calls": 22,
  "responder_calls": 20,
  "stage_metrics": {
    "interpreter": {
      "calls": 22,
      "input_tokens": 135833,
      "cached_read_tokens": 117766,
      "cache_write_tokens": 0,
      "uncached_tokens": 18067,
      "output_tokens": 6078,
      "latency_ms": 64457,
      "retries": 0,
      "fallback_calls": 0
    },
    "responder": {
      "calls": 20,
      "input_tokens": 30766,
      "cached_read_tokens": 1555,
      "cache_write_tokens": 29151,
      "uncached_tokens": 60,
      "output_tokens": 1676,
      "latency_ms": 34310,
      "retries": 0,
      "fallback_calls": 0
    },
    "all": {
      "calls": 42,
      "input_tokens": 166599,
      "cached_read_tokens": 119321,
      "cache_write_tokens": 29151,
      "uncached_tokens": 18127,
      "output_tokens": 7754,
      "latency_ms": 98767,
      "retries": 0,
      "fallback_calls": 0
    },
    "turn_latency_ms": 170171
  },
  "cost": {
    "actual_usd": 0.02260437,
    "without_explicit_cache_usd": 0.0426246,
    "saving_usd": 0.02002023,
    "saving_percent": 46.97
  },
  "stopped_for_deterministic_p0": false
}
