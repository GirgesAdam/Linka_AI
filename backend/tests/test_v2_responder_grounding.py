from langchain_core.messages import HumanMessage

from app.agents.v2.responder import _ensure_verified_doctor_list
from app.services.agent_v2.outcome import TurnOutcome


def _doctor_outcome() -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="answer_doctor",
        facts={
            "doctors": {
                "doctors": [
                    {"name": "د. سارة عادل"},
                    {"name": "د. عمر خليل"},
                    {"name": "د. نور علي"},
                ]
            }
        },
    )


def test_verified_doctor_list_appends_complete_grounded_list_when_model_omits_one() -> None:
    text = _ensure_verified_doctor_list(
        "الدكاترة المتاحين هم د. سارة عادل ود. عمر خليل.",
        history=[HumanMessage(content="مين الدكاترة اللي بيقدموا الخدمة؟")],
        outcomes=[_doctor_outcome()],
    )

    assert "د. نور علي" in text
    assert "وكمان من الدكاترة المطابقين" in text
    assert text.count("د. سارة عادل") == 1
    assert text.count("د. عمر خليل") == 1
    assert text.count("د. نور علي") == 1


def test_verified_doctor_list_leaves_complete_model_reply_unchanged() -> None:
    original = "الدكاترة هم د. سارة عادل، د. عمر خليل، ود. نور علي."

    text = _ensure_verified_doctor_list(
        original,
        history=[HumanMessage(content="مين الدكاترة اللي بيقدموا الخدمة؟")],
        outcomes=[_doctor_outcome()],
    )

    assert text == original


def test_verified_doctor_list_does_not_touch_availability_outcome() -> None:
    outcome = TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "doctors": {
                "doctors": [
                    {"name": "د. سارة عادل"},
                    {"name": "د. عمر خليل"},
                ]
            }
        },
    )
    original = "د. عمر خليل هو الأقرب."

    text = _ensure_verified_doctor_list(
        original,
        history=[HumanMessage(content="مين أقرب؟")],
        outcomes=[outcome],
    )

    assert text == original
