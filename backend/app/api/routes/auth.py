from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.security import (
    WorkspaceAccess,
    get_current_user,
    get_workspace_access,
    get_workspace_admin,
)
from app.database.session import get_db
from app.models.user import User
from app.models.workspace_member import WORKSPACE_ROLE_ADMIN, WORKSPACE_ROLE_MEMBER, WorkspaceMember
from app.schemas.auth import (
    CurrentUserRead,
    MeRead,
    WorkspaceAccessRead,
    WorkspaceInvitationCreate,
    WorkspaceInvitationRead,
    WorkspaceMemberRead,
    WorkspaceMemberRoleUpdate,
)
from app.schemas.page_permissions import MemberPagePermissionsRead, MemberPagePermissionsUpdate
from app.services.activity import record_activity_event
from app.services.page_permissions import (
    build_page_permissions_compare_and_swap,
    require_successful_permission_update,
)
from app.services.supabase_auth import SupabaseAuthError, invite_user_by_email

router = APIRouter()


def member_read(membership: WorkspaceMember, user: User) -> WorkspaceMemberRead:
    return WorkspaceMemberRead(
        membership_id=membership.id,
        user_id=user.id,
        auth_user_id=user.auth_user_id,
        email=user.email,
        full_name=user.full_name,
        role=membership.role,
        is_active=membership.is_active,
    )


def active_admin_count(db: Session, workspace_id: UUID) -> int:
    count = db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == WORKSPACE_ROLE_ADMIN,
            WorkspaceMember.is_active.is_(True),
        )
    )
    return int(count or 0)


def ensure_not_last_admin(
    db: Session,
    workspace_id: UUID,
    membership: WorkspaceMember,
) -> None:
    if (
        membership.role == WORKSPACE_ROLE_ADMIN
        and membership.is_active
        and active_admin_count(db, workspace_id) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace must always have at least one active admin.",
        )


@router.get("/me", response_model=MeRead)
def me(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeRead:
    memberships = list(
        db.scalars(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.is_active.is_(True),
            )
            .order_by(WorkspaceMember.created_at)
        )
    )

    workspaces = [
        WorkspaceAccessRead(
            workspace_id=membership.workspace.id,
            workspace_name=membership.workspace.name,
            workspace_slug=membership.workspace.slug,
            role=membership.role,
            is_demo=bool(membership.workspace.is_demo),
        )
        for membership in memberships
        if membership.workspace.is_active
    ]

    if user.auth_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Authenticated user is not linked to Supabase Auth.",
        )

    return MeRead(
        user=CurrentUserRead(
            id=user.id,
            auth_user_id=user.auth_user_id,
            email=user.email,
            full_name=user.full_name,
        ),
        workspaces=workspaces,
    )


@router.get("/workspace", response_model=WorkspaceAccessRead)
def current_workspace(
    access: WorkspaceAccess = Depends(get_workspace_access),
) -> WorkspaceAccessRead:
    return WorkspaceAccessRead(
        workspace_id=access.workspace.id,
        workspace_name=access.workspace.name,
        workspace_slug=access.workspace.slug,
        role=access.membership.role,
        is_demo=bool(access.workspace.is_demo),
    )


@router.get("/workspace/members", response_model=list[WorkspaceMemberRead])
def list_workspace_members(
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> list[WorkspaceMemberRead]:
    memberships = list(
        db.scalars(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == access.workspace.id)
            .order_by(WorkspaceMember.created_at)
        )
    )
    return [member_read(membership, membership.user) for membership in memberships]


@router.post(
    "/workspace/invitations",
    response_model=WorkspaceInvitationRead,
    status_code=status.HTTP_201_CREATED,
)
def invite_workspace_member(
    payload: WorkspaceInvitationCreate,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> WorkspaceInvitationRead:
    existing_user = db.scalar(select(User).where(User.email == payload.email))
    if existing_user is not None:
        existing_membership = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == access.workspace.id,
                WorkspaceMember.user_id == existing_user.id,
            )
        )
        if existing_membership is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This user already belongs to the workspace.",
            )
        if existing_user.auth_user_id is not None:
            membership = WorkspaceMember(
                workspace_id=access.workspace.id,
                user_id=existing_user.id,
                role=payload.role,
            )
            db.add(membership)
            db.flush()
            record_activity_event(
                db,
                workspace_id=access.workspace.id,
                actor_type="staff",
                actor_user_id=access.user.id,
                action="workspace.member_added",
                entity_type="workspace_member",
                entity_id=membership.id,
                summary="Workspace member added",
                metadata={
                    "target_user_id": existing_user.id,
                    "role": membership.role,
                    "invitation_sent": False,
                },
            )
            db.commit()
            db.refresh(membership)
            return WorkspaceInvitationRead(
                membership=member_read(membership, existing_user),
                invitation_sent=False,
            )

    try:
        invited = invite_user_by_email(payload.email)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Supabase could not create the invitation. If the user already exists in "
                "Supabase Auth, link them after their first sign-in."
            ),
        ) from exc

    if existing_user is None:
        existing_user = User(
            auth_user_id=invited.auth_user_id,
            email=invited.email,
        )
        db.add(existing_user)
        db.flush()
    else:
        existing_user.auth_user_id = invited.auth_user_id

    membership = WorkspaceMember(
        workspace_id=access.workspace.id,
        user_id=existing_user.id,
        role=payload.role,
    )
    db.add(membership)
    try:
        db.flush()
        record_activity_event(
            db,
            workspace_id=access.workspace.id,
            actor_type="staff",
            actor_user_id=access.user.id,
            action="workspace.member_added",
            entity_type="workspace_member",
            entity_id=membership.id,
            summary="Workspace member invited",
            metadata={
                "target_user_id": existing_user.id,
                "role": membership.role,
                "invitation_sent": True,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not create workspace membership.",
        ) from exc

    db.refresh(existing_user)
    db.refresh(membership)
    return WorkspaceInvitationRead(
        membership=member_read(membership, existing_user),
        invitation_sent=True,
    )


@router.patch("/workspace/members/{membership_id}", response_model=WorkspaceMemberRead)
def update_workspace_member_role(
    membership_id: UUID,
    payload: WorkspaceMemberRoleUpdate,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> WorkspaceMemberRead:
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.id == membership_id,
            WorkspaceMember.workspace_id == access.workspace.id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    if membership.role == WORKSPACE_ROLE_ADMIN and payload.role != WORKSPACE_ROLE_ADMIN:
        ensure_not_last_admin(db, access.workspace.id, membership)

    previous_role = membership.role
    membership.role = payload.role
    if membership.role != previous_role:
        record_activity_event(
            db,
            workspace_id=access.workspace.id,
            actor_type="staff",
            actor_user_id=access.user.id,
            action="workspace.member_role_changed",
            entity_type="workspace_member",
            entity_id=membership.id,
            summary="Workspace member role changed",
            metadata={
                "target_user_id": membership.user_id,
                "from_role": previous_role,
                "to_role": membership.role,
            },
        )
    db.commit()
    db.refresh(membership)
    return member_read(membership, membership.user)



@router.get(
    "/workspace/members/{membership_id}/page-permissions",
    response_model=MemberPagePermissionsRead,
)
def get_member_page_permissions(
    membership_id: UUID,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> MemberPagePermissionsRead:
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.id == membership_id,
            WorkspaceMember.workspace_id == access.workspace.id,
        )
    )
    if (
        membership is None
        or not membership.is_active
        or membership.role != WORKSPACE_ROLE_MEMBER
        or membership.user_id == access.user.id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    return MemberPagePermissionsRead(
        membership_id=membership.id,
        page_permissions=membership.page_permissions,
        permissions_version=membership.permissions_version,
        is_legacy=membership.page_permissions is None,
    )


@router.put(
    "/workspace/members/{membership_id}/page-permissions",
    response_model=MemberPagePermissionsRead,
)
def update_member_page_permissions(
    membership_id: UUID,
    payload: MemberPagePermissionsUpdate,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> MemberPagePermissionsRead:
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.id == membership_id,
            WorkspaceMember.workspace_id == access.workspace.id,
        )
    )
    if (
        membership is None
        or not membership.is_active
        or membership.role != WORKSPACE_ROLE_MEMBER
        or membership.user_id == access.user.id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    statement = build_page_permissions_compare_and_swap(
        workspace_id=access.workspace.id,
        membership_id=membership.id,
        actor_user_id=access.user.id,
        expected_version=payload.expected_version,
        page_permissions=payload.page_permissions,
    )
    updated_version = db.execute(statement).scalar_one_or_none()
    require_successful_permission_update(
        updated_version, expected_version=payload.expected_version
    )
    db.commit()
    db.refresh(membership)
    return MemberPagePermissionsRead(
        membership_id=membership.id,
        page_permissions=membership.page_permissions,
        permissions_version=membership.permissions_version,
        is_legacy=False,
    )


@router.delete("/workspace/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_workspace_member(
    membership_id: UUID,
    access: WorkspaceAccess = Depends(get_workspace_admin),
    db: Session = Depends(get_db),
) -> Response:
    membership = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.id == membership_id,
            WorkspaceMember.workspace_id == access.workspace.id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    ensure_not_last_admin(db, access.workspace.id, membership)

    record_activity_event(
        db,
        workspace_id=access.workspace.id,
        actor_type="staff",
        actor_user_id=access.user.id,
        action="workspace.member_removed",
        entity_type="workspace_member",
        entity_id=membership.id,
        summary="Workspace member removed",
        metadata={"target_user_id": membership.user_id, "role": membership.role},
    )
    db.delete(membership)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
