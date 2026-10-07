from pathlib import Path


def test_new_whatsapp_bookings_do_not_offer_reschedule_button() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/services/whatsapp_interactions.py").read_text(encoding="utf-8")
    producer = source.split("def whatsapp_booking_dispatch_metadata", 1)[1]

    assert '"title": "تغيير الميعاد"' not in producer
    assert '_RESCHEDULE_PREFIX' not in producer
    assert 'return dict(message.metadata_json or {})' in producer
    assert 'metadata["whatsapp_interactive"]' not in producer


def test_legacy_reschedule_button_reply_remains_parseable() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/services/whatsapp_interactions.py").read_text(encoding="utf-8")
    parser = source.split("def parse_whatsapp_booking_action", 1)[1].split("def _uuid", 1)[0]

    assert '_RESCHEDULE_PREFIX, "reschedule"' in parser
