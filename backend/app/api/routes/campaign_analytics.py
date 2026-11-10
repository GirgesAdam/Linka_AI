from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies.analytics import get_analytics_db, get_analytics_section_reader
from app.api.dependencies.security import WorkspaceAccess
from app.schemas.campaign_analytics import CampaignAnalyticsOverviewRead
from app.services.campaign_analytics import campaign_analytics_overview

router = APIRouter()
_ALLOWED_DAYS = {30, 90, 365}


@router.get("/campaigns", response_model=CampaignAnalyticsOverviewRead)
def get_campaign_analytics(
    access: Annotated[WorkspaceAccess, Depends(get_analytics_section_reader("administration"))],
    db: Annotated[Session, Depends(get_analytics_db)],
    days: Annotated[int | None, Query()] = 90,
    all_history: bool = False,
) -> CampaignAnalyticsOverviewRead:
    if all_history:
        days = None
    elif days not in _ALLOWED_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="days must be one of 30, 90, 365 or use all_history=true",
        )
    return campaign_analytics_overview(
        db,
        workspace_id=access.workspace.id,
        days=days,
    )


@router.get("/campaigns/{campaign_id}", response_model=CampaignAnalyticsOverviewRead)
def get_campaign_analytics_detail(
    campaign_id: UUID,
    access: Annotated[WorkspaceAccess, Depends(get_analytics_section_reader("administration"))],
    db: Annotated[Session, Depends(get_analytics_db)],
) -> CampaignAnalyticsOverviewRead:
    result = campaign_analytics_overview(
        db,
        workspace_id=access.workspace.id,
        days=None,
        campaign_id=campaign_id,
        limit=1,
    )
    if not result.campaigns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return result
