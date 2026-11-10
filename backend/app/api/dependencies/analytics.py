from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

from app.api.dependencies.security import (
    WorkspaceAccess,
    get_current_user,
    get_verified_identity,
    get_workspace_access,
)
from app.database.session import AnalyticsSessionLocal, SessionLocal
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WORKSPACE_ROLE_MEMBER
from app.services.analytics_capacity import is_statement_timeout
from app.services.section_access import SectionAccessKey, section_access_allows
from app.services.supabase_auth import VerifiedAuthIdentity


def get_analytics_workspace_reader(
    x_workspace_id: Annotated[UUID, Header(alias="X-Workspace-ID")],
    identity: Annotated[VerifiedAuthIdentity, Depends(get_verified_identity)],
) -> WorkspaceAccess:
    """Resolve workspace access without holding the main DB pool during reports.

    User/profile synchronization still uses the normal operational pool because
    it may perform a small write on first login. The session is closed before
    the analytics route starts its potentially heavier read workload.
    """
    with SessionLocal() as auth_db:
        user = get_current_user(identity, auth_db)
        access = get_workspace_access(x_workspace_id, user, auth_db)
        if access.membership.role not in {WORKSPACE_ROLE_ADMIN, WORKSPACE_ROLE_MEMBER}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your workspace role does not allow this action.",
            )
        # Session.close() detaches the already-loaded ORM objects. With
        # expire_on_commit=False their scalar fields remain available to the
        # route without keeping an operational connection checked out.
        return access


def get_analytics_db() -> Generator[Session, None, None]:
    """Dedicated, bounded DB dependency for read-heavy analytics endpoints."""
    db = AnalyticsSessionLocal()
    try:
        yield db
    except SQLAlchemyTimeoutError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="التحليلات مشغولة دلوقتي. جرّب تاني بعد لحظة.",
        ) from exc
    except DBAPIError as exc:
        db.rollback()
        if is_statement_timeout(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="التحليل أخد وقت أطول من الحد الآمن. جرّب فترة أقصر أو فلاتر أضيق.",
            ) from exc
        raise
    finally:
        db.close()


def get_analytics_section_reader(
    section: SectionAccessKey,
) -> Callable[..., WorkspaceAccess]:
    """Enforce section access while releasing the primary DB session before analytics queries."""
    if section not in {"clinic_operations", "administration"}:
        raise ValueError(f"Unknown member access section: {section!r}")

    def dependency(
        x_workspace_id: Annotated[UUID, Header(alias="X-Workspace-ID")],
        identity: Annotated[VerifiedAuthIdentity, Depends(get_verified_identity)],
    ) -> WorkspaceAccess:
        with SessionLocal() as auth_db:
            user = get_current_user(identity, auth_db)
            access = get_workspace_access(x_workspace_id, user, auth_db)
            if not section_access_allows(
                access.membership,
                workspace_id=access.workspace.id,
                user_id=access.user.id,
                section=section,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to access this section.",
                )
            return access

    return dependency
