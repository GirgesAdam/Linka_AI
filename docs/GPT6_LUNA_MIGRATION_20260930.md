# Linka GPT-6 Luna Migration

Date: 2026-09-30

Starting main SHA:
01a14d0ab8d7fb28b83cc0e6a754797bb06b914f

## Scope

This task changes Linka's current/default primary model from `gpt-5.6-luna` to `gpt-6-luna`.
It does not change prompts, planner behavior, tools, business rules, Response Contract ownership,
fallback architecture, or reasoning effort.

## API compatibility audit

Current Linka runtime:
- provider: OpenAI only;
- transport: Responses API via LangChain `ChatOpenAI(use_responses_api=True)`;
- structured output: existing LangChain structured-output path;
- tool/function calling: existing bound-tool path;
- primary reasoning effort: `low`;
- provider response storage: disabled (`store=False`).

GPT-6 Luna compatibility:
- Responses API: supported;
- function calling: supported;
- structured outputs: supported;
- reasoning effort `low`: supported;
- explicit prompt-cache family gate already treats GPT 6 as supported.

Decision:
Keep the Responses API and `low` reasoning unchanged. No Chat Completions compatibility adapter
or endpoint migration is required.

## Reference audit

Current/default references migrated:
- `backend/app/core/config.py`;
- `.env.example`;
- current GitHub Actions agent-eval/live-validation workflow defaults;
- current-model test expectations;
- prompt-cache current-model fixtures.

Historical evidence intentionally unchanged:
- `backend/eval_results/**`;
- dated evaluation/closeout reports under `docs/**`;
- historical model labels embedded in immutable evidence.

## Production override

Railway production defines service-level variables named:
- `OPENAI_MODEL`;
- `OPENAI_REASONING_EFFORT`.

The connected Railway client redacts their current values. Therefore cutover requires explicitly
setting `OPENAI_MODEL=gpt-6-luna` after merge and proving the effective runtime model from live
response/model telemetry. `OPENAI_REASONING_EFFORT` remains unchanged.

## Existing fallback

The repository already has a cross-model `gpt-5-mini` fallback for selected provider failures.
This migration does not add, remove, or retune fallback behavior.

## Validation contract

Material regression counters:
- semantic_regressions = 0
- wrong_tool_calls = 0
- wrong_writes = 0
- structured_output_failures = 0
- grounding_failures = 0
- choice_regressions = 0
- mixed_truth_regressions = 0

Provider retries/fallbacks and token/latency/cost evidence are recorded from bounded live validation.

## GPT-5.6 Luna baseline evidence

Latest retained Batch 5 evidence (historical, not rewritten):
- model: gpt-5.6-luna
- reasoning effort: low
- LLM calls: 40 across 20 turns
- input tokens: 154395
- cached-read tokens: 101707
- cache-write tokens: 36075
- uncached input tokens: 16613
- output tokens: 7123
- total tokens: 161518
- aggregate LLM latency: 126935 ms
- provider retries: 0
- fallback calls: 0
- recorded actual cost: $0.02292309

Using the same retained token mix only as a cost-normalized estimate, current GPT-6 Luna
short-context pricing would be approximately $0.00537462. This is not a claim that GPT-6
will emit the same token counts; candidate runtime telemetry remains the authority after cutover.

Pre-cutover production generic-turn smoke:
- prompt: Arabic thank-you acknowledgement;
- effective model label: openai:gpt-5.6-luna;
- browser smoke runtime: 13.60 s including login/navigation;
- console errors: 0.

