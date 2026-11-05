from __future__ import annotations

from io import BytesIO
from uuid import UUID

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.historical_import import ClinicHistoricalImportRow


# Preview messages for these codes contain the patient's phone or customer IDs.
# Retain the original staged issue_message; sanitize only the exported report.
_SAFE_IDENTITY_REASONS = {
    "customer_id_conflict_in_file": (
        "يوجد تعارض بين أرقام تعريف العميل في ملف الاستيراد. "
        "راجع بيانات الصفوف المرتبطة بنفس العميل."
    ),
    "customer_id_conflict_existing_patient": (
        "رقم تعريف العميل في الملف مختلف عن رقم التعريف المسجل للعميل الموجود في النظام."
    ),
}


def _text_cell(sheet, value: str | None) -> WriteOnlyCell:
    """Keep untrusted spreadsheet text literal, including formula-like prefixes."""
    cell = WriteOnlyCell(sheet, value=ILLEGAL_CHARACTERS_RE.sub(" ", value or ""))
    cell.data_type = "s"
    cell.number_format = "@"
    return cell


def build_historical_import_error_report(
    db: Session, *, workspace_id: UUID, batch_id: UUID
) -> bytes:
    """Export every rejected staged row; never read or export patient payloads."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Rejected Rows")
    sheet.freeze_panes = "A2"
    sheet.append(["Sheet", "Row Number", "Entity Type", "Issue Code", "Reason"])

    statement = (
        select(
            ClinicHistoricalImportRow.source_sheet,
            ClinicHistoricalImportRow.row_number,
            ClinicHistoricalImportRow.entity_type,
            ClinicHistoricalImportRow.issue_code,
            ClinicHistoricalImportRow.issue_message,
        )
        .where(
            ClinicHistoricalImportRow.workspace_id == workspace_id,
            ClinicHistoricalImportRow.batch_id == batch_id,
            ClinicHistoricalImportRow.row_status == "rejected",
        )
        .order_by(
            ClinicHistoricalImportRow.source_sheet,
            ClinicHistoricalImportRow.row_number,
            ClinicHistoricalImportRow.entity_type,
            ClinicHistoricalImportRow.id,
        )
        .execution_options(yield_per=1000)
    )

    output = BytesIO()
    try:
        for source_sheet, row_number, entity_type, issue_code, issue_message in db.execute(statement):
            sheet.append([
                _text_cell(sheet, source_sheet),
                row_number,
                _text_cell(sheet, entity_type),
                _text_cell(sheet, issue_code),
                _text_cell(sheet, _SAFE_IDENTITY_REASONS.get(issue_code, issue_message)),
            ])
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()
        output.close()
