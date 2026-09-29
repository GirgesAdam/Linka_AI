from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink

AuthorityOwner = Literal["tia", "external"]

AUTHORITY_DOMAINS = ("patients", "payments", "appointments")
AUTHORITY_OWNERS = ("tia", "external")
PATIENT_EXTERNAL_SYNC_FIELDS = (
    "first_name",
    "last_name",
    "phone",
    "gender",
    "birth_date",
    "source_created_at",
    "status",
    "preferred_language",
    "source",
)


class ClinicIntegrationAuthorityError(ValueError):
    pass


def default_authority_policy_for_mode(mode: str) -> dict[str, Any]:
    """Return conservative defaults without inventing write ownership.

    Continuous external/hybrid integrations own patient/payment sync by default.
    Appointment authority remains Tia-owned by default and must be opted into
    explicitly after the external appointment sync contract is installed.
    """
    external_sync = mode in {"external_api", "hybrid"}
    return {
        "patients": {"owner": "external" if external_sync else "tia", "fields": {}},
        "payments": {"owner": "external" if external_sync else "tia", "fields": {}},
        "appointments": {"owner": "tia", "fields": {}},
    }


def normalize_authority_policy(raw: object, *, mode: str) -> dict[str, Any]:
    base = default_authority_policy_for_mode(mode)
    if raw in (None, {}):
        return base
    if not isinstance(raw, dict):
        raise ClinicIntegrationAuthorityError("Integration authority policy must be an object.")

    normalized = deepcopy(base)
    unknown_domains = set(raw) - set(AUTHORITY_DOMAINS)
    if unknown_domains:
        raise ClinicIntegrationAuthorityError("Integration authority policy contains an unsupported domain.")

    for domain in AUTHORITY_DOMAINS:
        domain_raw = raw.get(domain)
        if domain_raw is None:
            continue
        if not isinstance(domain_raw, dict):
            raise ClinicIntegrationAuthorityError(f"Authority policy for {domain} must be an object.")
        unknown_keys = set(domain_raw) - {"owner", "fields"}
        if unknown_keys:
            raise ClinicIntegrationAuthorityError(f"Authority policy for {domain} contains unsupported keys.")
        owner = str(domain_raw.get("owner", normalized[domain]["owner"])).strip().lower()
        if owner not in AUTHORITY_OWNERS:
            raise ClinicIntegrationAuthorityError(f"Authority owner for {domain} must be tia or external.")
        if domain == "appointments" and owner == "external" and mode not in {"external_api", "hybrid"}:
            raise ClinicIntegrationAuthorityError(
                "External appointment authority requires external_api or hybrid integration mode."
            )

        fields_raw = domain_raw.get("fields", {})
        if not isinstance(fields_raw, dict):
            raise ClinicIntegrationAuthorityError(f"Field authority for {domain} must be an object.")
        if domain != "patients" and fields_raw:
            raise ClinicIntegrationAuthorityError(
                f"Field-level authority for {domain} is not supported until that write contract exists."
            )

        fields: dict[str, AuthorityOwner] = {}
        for field_name, field_owner_raw in fields_raw.items():
            field_name = str(field_name).strip()
            if field_name not in PATIENT_EXTERNAL_SYNC_FIELDS:
                raise ClinicIntegrationAuthorityError("Patient authority policy contains an unsupported field.")
            field_owner = str(field_owner_raw).strip().lower()
            if field_owner not in AUTHORITY_OWNERS:
                raise ClinicIntegrationAuthorityError("Patient field authority owner must be tia or external.")
            fields[field_name] = field_owner  # type: ignore[assignment]

        normalized[domain] = {"owner": owner, "fields": fields}
    return normalized


def integration_authority_policy(integration: ClinicIntegration) -> dict[str, Any]:
    return normalize_authority_policy(
        getattr(integration, "authority_policy_json", None) or {},
        mode=integration.mode,
    )


def domain_authority_owner(integration: ClinicIntegration, domain: str) -> AuthorityOwner:
    if domain not in AUTHORITY_DOMAINS:
        raise ClinicIntegrationAuthorityError("Unsupported integration authority domain.")
    return integration_authority_policy(integration)[domain]["owner"]


def patient_field_authority_owner(
    integration: ClinicIntegration,
    field_name: str,
) -> AuthorityOwner:
    if field_name not in PATIENT_EXTERNAL_SYNC_FIELDS:
        return "tia"
    policy = integration_authority_policy(integration)["patients"]
    return policy["fields"].get(field_name, policy["owner"])


def external_patient_fields(integration: ClinicIntegration) -> frozenset[str]:
    return frozenset(
        field_name
        for field_name in PATIENT_EXTERNAL_SYNC_FIELDS
        if patient_field_authority_owner(integration, field_name) == "external"
    )


def external_domain_write_enabled(integration: ClinicIntegration, domain: str) -> bool:
    if domain == "patients":
        return bool(external_patient_fields(integration))
    return domain_authority_owner(integration, domain) == "external"


def require_external_domain_authority(integration: ClinicIntegration, domain: str) -> None:
    if not external_domain_write_enabled(integration, domain):
        raise ClinicIntegrationAuthorityError(
            f"External sync is not authoritative for the {domain} domain."
        )


def require_tia_domain_authority(integration: ClinicIntegration, domain: str) -> None:
    if domain_authority_owner(integration, domain) != "tia":
        raise ClinicIntegrationAuthorityError(
            f"Linka is not authoritative for the {domain} domain."
        )


def require_tia_patient_fields_writable(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    fields: set[str],
) -> None:
    managed = set(fields) & set(PATIENT_EXTERNAL_SYNC_FIELDS)
    if not managed:
        return
    link = db.scalar(
        select(ClinicIntegrationEntityLink.id).where(
            ClinicIntegrationEntityLink.workspace_id == workspace_id,
            ClinicIntegrationEntityLink.entity_type == "patient",
            ClinicIntegrationEntityLink.canonical_id == str(patient_id),
        )
    )
    if link is None:
        return
    integration = db.get(ClinicIntegration, workspace_id)
    if integration is None:
        return
    blocked = sorted(
        field_name
        for field_name in managed
        if patient_field_authority_owner(integration, field_name) == "external"
    )
    if blocked:
        raise ClinicIntegrationAuthorityError(
            "These patient fields are owned by the external clinic system: " + ", ".join(blocked)
        )


def require_tia_workspace_domain_write(
    db: Session,
    *,
    workspace_id: UUID,
    domain: str,
) -> None:
    integration = db.get(ClinicIntegration, workspace_id)
    if integration is None:
        return
    require_tia_domain_authority(integration, domain)
