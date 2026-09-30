from pathlib import Path

from app.core.config import Settings


def test_realtime_defaults_use_luna_then_gpt5_mini() -> None:
    assert Settings.model_fields["openai_model"].default == "gpt-6-luna"
    assert Settings.model_fields["openai_fallback_model"].default == "gpt-5-mini"
    assert Settings.model_fields["openai_reasoning_effort"].default == "low"
    assert Settings.model_fields["openai_fallback_reasoning_effort"].default == "low"
    assert Settings.model_fields["llm_realtime_max_retries"].default == 0


def test_realtime_model_chain_source_has_no_customer_text_heuristics() -> None:
    backend = Path(__file__).resolve().parent.parent
    runtime = (backend / "app/agents/llm_runtime.py").read_text(encoding="utf-8").lower()
    turn = (backend / "app/agents/turn_interpreter.py").read_text(encoding="utf-8").lower()

    # Regex in llm_runtime is allowed only for provider-secret redaction; customer
    # language interpretation remains semantic/LLM-owned rather than lexical.
    assert "_sensitive_provider_patterns" in runtime
    for token in ("re.search", "re.match", "difflib."):
        assert token not in runtime
    for token in ("re.compile", "re.search", "re.match", "difflib."):
        assert token not in turn
