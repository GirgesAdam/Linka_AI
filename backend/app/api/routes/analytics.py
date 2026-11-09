from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.analytics import get_analytics_db, get_analytics_page_reader
from app.api.dependencies.security import WorkspaceAccess
from app.schemas.analytics import AnalyticsOverviewRead
from app.schemas.analytics_catalog import (
    AnalyticsCatalogRead,
    AnalyticsCatalogRunRead,
    AnalyticsCatalogRunRequest,
)
from app.schemas.patient_history import HistoricalAnalyticsRead
from app.services.analytics import ANALYTICS_ALLOWED_DAYS, analytics_overview
from app.services.analytics_bi import AnalyticsBIError
from app.services.analytics_catalog import analytics_catalog, run_catalog_analysis
from app.services.analytics_export import AnalyticsExportLimitError, analytics_result_csv
from app.services.patient_history import historical_analytics

router = APIRouter()


@router.get("/overview", response_model=AnalyticsOverviewRead)
def get_analytics_overview(
    access: Annotated[WorkspaceAccess, Depends(get_analytics_page_reader("analytics.overview"))],
    db: Annotated[Session, Depends(get_analytics_db)],
    days: int = 30,
) -> AnalyticsOverviewRead:
    if days not in ANALYTICS_ALLOWED_DAYS:
        raise HTTPException(status_code=422, detail="days must be one of 7, 30, 90")
    return analytics_overview(
        db,
        workspace_id=access.workspace.id,
        timezone_name=access.workspace.timezone,
        days=days,
    )


@router.get("/history", response_model=HistoricalAnalyticsRead)
def get_historical_analytics(
    access: Annotated[WorkspaceAccess, Depends(get_analytics_page_reader("analytics.overview"))],
    db: Annotated[Session, Depends(get_analytics_db)],
) -> HistoricalAnalyticsRead:
    return historical_analytics(db, workspace_id=access.workspace.id)


@router.get("/catalog", response_model=AnalyticsCatalogRead)
def get_analytics_catalog(
    access: Annotated[WorkspaceAccess, Depends(get_analytics_page_reader("analytics.overview"))],
    db: Annotated[Session, Depends(get_analytics_db)],
) -> AnalyticsCatalogRead:
    """Return the admin-selectable analytics library."""
    return analytics_catalog(db, workspace_id=access.workspace.id)


@router.post("/catalog/run", response_model=AnalyticsCatalogRunRead)
def run_selected_analytics(
    payload: AnalyticsCatalogRunRequest,
    access: Annotated[WorkspaceAccess, Depends(get_analytics_page_reader("analytics.overview"))],
    db: Annotated[Session, Depends(get_analytics_db)],
) -> AnalyticsCatalogRunRead:
    """Execute one registered analytics function with explicit validated filters."""
    timezone_name = (access.workspace.timezone or "Africa/Cairo").strip()
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Africa/Cairo")
    try:
        return run_catalog_analysis(
            db,
            workspace_id=access.workspace.id,
            request=payload,
            now=datetime.now(tz),
        )
    except AnalyticsBIError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/catalog/export")
def export_selected_analytics(
    payload: AnalyticsCatalogRunRequest,
    access: Annotated[WorkspaceAccess, Depends(get_analytics_page_reader("analytics.overview"))],
    db: Annotated[Session, Depends(get_analytics_db)],
) -> Response:
    """Re-run the selected analysis server-side and export the current result."""
    timezone_name = (access.workspace.timezone or "Africa/Cairo").strip()
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Africa/Cairo")
    try:
        result = run_catalog_analysis(
            db,
            workspace_id=access.workspace.id,
            request=payload,
            now=datetime.now(tz),
            use_cache=False,
        )
    except AnalyticsBIError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        content = analytics_result_csv(result)
    except AnalyticsExportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    filename = f"tia-{result.analysis_key}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
