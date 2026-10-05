# Linka — F1 Fresh Runtime Acceptance: Nearest Availability

Date: 2026-10-05

## Scope

Focused acceptance for PR #202 only: cross-turn nearest-availability continuation after a verified no-availability result, plus workspace-local customer date presentation. No merge or deploy was performed during this acceptance.

Starting main: `d13c4b8724592de5f5c6bd8faa17ea0a1d99ad64`
Initial reviewed PR head: `c1095efe03948b87ddb6417d8c1f3dea050f227c`

## Root cause and deterministic regression

Before the fix, `if_previous_no_availability` inspected only `previous_reads`, which represents a predecessor operation in the same turn. The immediately previous cross-turn verified read was available in server-owned `recent_read_context`, but was ignored by the condition gate.

Pre-patch reproduction on the real helper:

```text
same-turn verified zero  -> True
cross-turn previous_reads=None + recent verified zero -> False
```

The implemented guard preserves same-turn evidence as highest priority. Cross-turn evidence is accepted only from the immediately previous server-owned availability/book/reschedule context with strict integer `availability_option_count == 0`.

Fresh acceptance also exposed one semantic variation: `continues_previous=true + condition=always` could carry the already-missed exact date back into the active task. The final narrow normalization advances only a date-level verified miss when the first cross-turn continuation adds no new time scope (or typed `nearest`). Positive recent availability, time-scoped misses, explicit different dates, and same-turn operations are not rewritten.

## Acceptance environment

- Runtime: actual V2 agent runtime through `tia-agent-eval` environment variables.
- Database safety: one outer transaction with rollback; scenario fixtures also use savepoints.
- Production WhatsApp traffic: not used.
- Business writes: none.

## R1–R8 actual runtime evidence

### R1

**Customer:** ممكن أحجز Under Arm على DEKA النهارده؟

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه.

مفيش مواعيد متاحة النهارده. أقدر أدورلك في يوم تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-05",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog",
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-05"
    ],
    "window_dates": []
  }
}
```

**Customer:** طيب أول ميعاد فاضي بعد النهارده إمتى؟

**Linka:** لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع مريم حسن بكرة: من 10 صباحًا لـ5 مساءً.
• DEKA Again مع يوسف سمير بكرة: من 12 ظهرًا لـ12:15 ظهرًا، ومن 1:15 مساءً لـ8 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "from_date",
      "date": {
        "mode": "from_date",
        "start_date": "2026-10-06",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-05"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 59,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": [
      "2026-10-06",
      "2026-10-06",
      "2026-10-06"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

### R2

**Customer:** عايز أحجز Under Arm على DEKA

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": null,
      "date": null,
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": null
}
```

**Customer:** طب بكرة فيه مكان؟

**Linka:** مفيش مواعيد متاحة بكرة لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-06",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": []
  }
}
```

**Customer:** واللي بعدها أقرب حاجة إمتى؟

**Linka:** المتاح عندنا لخدمة Under Arm:
• DEKA Again مع مريم حسن يوم الأربعاء 7 أكتوبر: من 10 صباحًا لـ11:15 صباحًا، ومن 12:15 ظهرًا لـ5 مساءً.
• DEKA Again مع أحمد محمود يوم الأربعاء 7 أكتوبر: من 12 ظهرًا لـ9 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "from_date",
      "date": {
        "mode": "from_date",
        "start_date": "2026-10-07",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "continue_active",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 63,
    "checked_dates": [
      "2026-10-07"
    ],
    "window_dates": [
      "2026-10-07",
      "2026-10-07",
      "2026-10-07"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

### R3

**Customer:** محتاج Under Arm على DEKA الخميس

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه.

مفيش مواعيد متاحة يوم الخميس 8 أكتوبر. أقدر أدورلك في يوم تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-08",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog",
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-08"
    ],
    "window_dates": []
  }
}
```

**Customer:** طب بعد الخميس أول حاجة فاضية إمتى؟

**Linka:** لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع أحمد محمود يوم الجمعة 9 أكتوبر: من 2 مساءً لـ5:15 مساءً، ومن 6:15 مساءً لـ9 مساءً.
• DEKA Again مع يوسف سمير يوم الجمعة 9 أكتوبر: من 2 مساءً لـ8 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "from_date",
      "date": {
        "mode": "from_date",
        "start_date": "2026-10-09",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-08"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 51,
    "checked_dates": [
      "2026-10-09"
    ],
    "window_dates": [
      "2026-10-09",
      "2026-10-09",
      "2026-10-09"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

### R4

**Customer:** عايز Under Arm على DEKA بكرة بعد 7 بالليل

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه.

مفيش مواعيد متاحة بكرة. أقدر أدورلك في يوم تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-06",
        "end_date": null
      },
      "time": {
        "mode": "after",
        "start_time": "19:00",
        "end_time": null,
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none"
      }
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog",
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": []
  }
}
```

**Customer:** طب أقرب ميعاد بعد كده؟

**Linka:** المتاح عندنا:
• DEKA Again مع أحمد محمود يوم الأربعاء 7 أكتوبر: من 7 مساءً لـ9 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": true,
      "continuation_condition": "if_previous_no_availability",
      "date_mode": "next_available",
      "date": {
        "mode": "next_available",
        "start_date": null,
        "end_date": null
      },
      "time": {
        "mode": "after",
        "start_time": "19:00",
        "end_time": null,
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none"
      }
    }
  ],
  "recent_verified_read": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 9,
    "checked_dates": [
      "2026-10-05",
      "2026-10-06",
      "2026-10-07"
    ],
    "window_dates": [
      "2026-10-07"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

### R5

**Customer:** عايز أحجز Under Arm على DEKA

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه. تحب تحجز يوم إيه؟

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": null,
      "date": null,
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": null
}
```

**Customer:** الأربع مناسب؟

**Linka:** لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع مريم حسن يوم الأربعاء 7 أكتوبر: من 10 صباحًا لـ11:15 صباحًا، ومن 12:15 ظهرًا لـ5 مساءً.
• DEKA Again مع أحمد محمود يوم الأربعاء 7 أكتوبر: من 12 ظهرًا لـ9 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-07",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 63,
    "checked_dates": [
      "2026-10-07"
    ],
    "window_dates": [
      "2026-10-07",
      "2026-10-07",
      "2026-10-07"
    ]
  }
}
```

**Customer:** لا خليه الجمعة

**Linka:** مفيش مواعيد متاحة يوم الجمعة 9 أكتوبر لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-09",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "continue_active",
    "availability_option_count": 63,
    "checked_dates": [
      "2026-10-07"
    ],
    "window_dates": [
      "2026-10-07",
      "2026-10-07",
      "2026-10-07"
    ]
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-09"
    ],
    "window_dates": []
  }
}
```

**Customer:** طب أقرب يوم بعدها؟

**Linka:** لقيتلك المواعيد دي لخدمة Under Arm:
• DEKA Again مع يوسف سمير يوم السبت 10 أكتوبر: من 12 ظهرًا لـ1:15 مساءً، ومن 2:15 مساءً لـ8 مساءً.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "from_date",
      "date": {
        "mode": "from_date",
        "start_date": "2026-10-10",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "continue_active",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-09"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 30,
    "checked_dates": [
      "2026-10-10"
    ],
    "window_dates": [
      "2026-10-10",
      "2026-10-10"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

### R6

Deterministic negative control (positive recent availability).

```json
{
  "operations": [
    {
      "type": "availability",
      "continues_previous": true,
      "continuation_condition": "if_previous_no_availability",
      "date_mode": null
    }
  ],
  "recent_verified_read": {
    "operation_type": "availability",
    "availability_option_count": 2
  },
  "same_turn_previous_read_present": false,
  "step_skipped": true,
  "condition_satisfied": false,
  "write_attempted": false,
  "error": null
}
```

Result: **PASS** — positive recent availability did not satisfy `if_previous_no_availability`.

### R7

**Customer:** ممكن Under Arm على DEKA النهارده؟

**Linka:** مفيش مواعيد متاحة النهارده لخدمة Under Arm. أقدر أدورلك في نطاق تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "availability",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-05",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "availability",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-05"
    ],
    "window_dates": []
  }
}
```

Workspace boundary evidence:

```json
{
  "utc_now": "2026-10-04T22:30:00+00:00",
  "workspace_local_now": "2026-10-05T01:30:00+03:00",
  "direct_formatter_oct5": "النهارده"
}
```

Result: **PASS** — errors=[], writes=0.

### R8

**Customer:** عايز Under Arm على DEKA النهارده

**Linka:** جلسة Under Arm على DEKA Again سعرها 550 جنيه.

مفيش مواعيد متاحة النهارده. أقدر أدورلك في يوم تاني لو تحب.

```json
{
  "operation": [
    {
      "type": "book",
      "continues_previous": false,
      "continuation_condition": "always",
      "date_mode": "exact",
      "date": {
        "mode": "exact",
        "start_date": "2026-10-05",
        "end_date": null
      },
      "time": null
    }
  ],
  "recent_verified_read": null,
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "service_catalog",
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-05"
    ],
    "window_dates": []
  }
}
```

**Customer:** مفيش حاجة بعدها على طول؟

**Linka:** المتاح عندنا لخدمة Under Arm:
• DEKA Again مع مريم حسن بكرة: الساعة 10 صباحًا.
أنهي وقت أنسب لك؟

```json
{
  "operation": [
    {
      "type": "continue_active",
      "continues_previous": true,
      "continuation_condition": "always",
      "date_mode": "from_date",
      "date": {
        "mode": "from_date",
        "start_date": "2026-10-06",
        "end_date": null
      },
      "time": {
        "mode": "nearest",
        "start_time": null,
        "end_time": null,
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none"
      }
    }
  ],
  "recent_verified_read": {
    "operation_type": "book",
    "availability_option_count": 0,
    "checked_dates": [
      "2026-10-05"
    ],
    "window_dates": []
  },
  "same_turn_previous_read_present": false,
  "step_skipped": false,
  "verified_read_kinds": [
    "availability"
  ],
  "write_attempted": false,
  "error": null,
  "availability_result": {
    "operation_type": "continue_active",
    "availability_option_count": 1,
    "checked_dates": [
      "2026-10-06"
    ],
    "window_dates": [
      "2026-10-06"
    ]
  }
}
```

Result: **PASS** — errors=[], writes=0.

## Acceptance summary

| Control | Result | Key evidence |
|---|---|---|
| R1 | PASS | `النهارده` miss → verified `بكرة` availability; no write |
| R2 | PASS | `بكرة` miss → Wednesday 7 October; no backward result |
| R3 | PASS | Thursday 8 October miss → Friday 9 October |
| R4 | PASS | Tomorrow after 19:00 miss → next verified result still after 19:00 |
| R5 | PASS | Wednesday positive read replaced by Friday zero read; continuation starts Saturday, never jumps back |
| R6 | PASS | positive recent availability does not satisfy conditional fallback |
| R7 | PASS | UTC still Oct 4 while Cairo is Oct 5 → customer sees `النهارده` |
| R8 | PASS | fresh Egyptian nearest wording with `condition=always` advances from Oct 5 to Oct 6 using verified context |

Final acceptance counters:

```text
RuntimeError = 0
zero-outcome hard stops = 0
wrong writes = 0
unexpected writes = 0
backward-date results = 0
invented availability = 0
```

Relative-date evidence:

```text
local today    -> النهارده
local tomorrow -> بكرة
later date     -> weekday + day + month
UTC/local boundary -> workspace local date wins
```

## Final verdict

**F1 FRESH RUNTIME ACCEPTANCE = PASS**

PR #202 remains Draft pending final exact-head Shared CI and review. No merge or deployment performed.