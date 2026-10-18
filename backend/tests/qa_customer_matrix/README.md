# Reduced live customer QA selection

Runtime baseline: 0a2885809efe6dec7d6bc184abc158725228faba (PR #262).

This audit-only branch adds tests and a GitHub Actions workflow. Runtime application files and existing evaluators remain those of the baseline.

The run selects twelve distinct fixture conversations from the 194-case design: clinic facts, service description, doctors, two-device pricing, package pricing, clearing a doctor preference, human handoff, urgent medical handoff, own profile, language switching, nearest availability, and owned packages versus offers.

It also executes eight existing backend audit conversations once: C7 replacement and RW-2, RW-5, RW-6, RW-8, RW-9, RW-10, RW-11. These cover service addition/removal, confirm-plus-modify, compound visits, relative dates, spoken time, device choice, and explicit confirmation at the write boundary.

There are twenty conversations in total, without lexical variants or automatic scenario reruns. Model-internal structured-output retry behavior is unchanged.

The workflow uses secrets.OPENAI_API_KEY within GitHub Actions, a disposable local PostgreSQL service, and synthetic rollback fixtures. No external database credential is used. No appointment writes are executed. This run does not prove actual booking persistence, channel delivery, or the remaining integration cases.

Each fixture run stores state, plan, outcomes, and replies in a unique evidence directory. An assertion failure creates an investigation draft requiring triage. Backend audit logs retain raw model and normalization evidence. Passing structural checks still requires reply review.
