from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.api.routes.inbox import inbox_revision, inbox_summary


class _ScalarSession:
    def __init__(self, value: int) -> None:
        self.value = value
        self.statement = None

    def scalar(self, statement):
        self.statement = statement
        return self.value


class _ExecuteResult:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row

    def one(self):
        return self.row


class _ExecuteSession:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _ExecuteResult(self.row)


def _access():
    return SimpleNamespace(workspace=SimpleNamespace(id=uuid4()))


def test_inbox_summary_uses_count_only_query() -> None:
    db = _ScalarSession(4)
    result = inbox_summary(access=_access(), db=db)

    assert result.unread_conversations == 4
    assert db.statement is not None
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "count(*)" in sql.lower()
    assert "conversations.unread_count >" in sql
    assert "messages" not in sql.lower()
    assert "handoff_requests" not in sql.lower()


def test_inbox_revision_builds_stable_opaque_workspace_revision() -> None:
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    db = _ExecuteSession((12, now, now, now, now, None, now))

    result = inbox_revision(access=_access(), db=db, conversation_id=None)

    assert result.revision.startswith("12|")
    assert result.revision.count("|") == 6
    assert db.statement is not None
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "max(conversations.updated_at)" in sql
    assert "max(messages.updated_at)" in sql
    assert "max(patients.updated_at)" in sql
    assert "max(users.updated_at)" in sql
    assert "max(handoff_requests.updated_at)" in sql
    assert "max(channel_connections.updated_at)" in sql


def test_inbox_revision_tracks_conversation_messages_and_handoffs() -> None:
    now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    db = _ExecuteSession((now, 9, now, now, now, now, now, now, now))

    result = inbox_revision(access=_access(), db=db, conversation_id=uuid4())

    assert "|9|" in result.revision
    assert db.statement is not None
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "max(messages.updated_at)" in sql
    assert "patients.updated_at" in sql
    assert "users.updated_at" in sql
    assert "max(workspace_members.updated_at)" in sql
    assert "max(handoff_requests.updated_at)" in sql
    assert "max(handoff_events.created_at)" in sql
