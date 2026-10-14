from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.v2.turn_contract import ClockSource, DateConstraint, PackageUsage, TimeConstraint

TaskStatus = Literal["collecting", "awaiting_choice", "ready", "executing"]
LegacyPulseUsage = Literal["unspecified", "use_existing", "avoid_existing"]
ChoicePurpose = Literal[
    "service",
    "doctor",
    "branch",
    "device",
    "appointment",
    "appointment_target",
    "package",
    "booking_slot",
    "reschedule_slot",
]
LifecycleAction = Literal["cancel_appointment", "confirm_appointment", "reschedule"]
WriteOperation = Literal["booking", "reschedule"]


class StrictStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteAuthorization(StrictStateModel):
    operation: WriteOperation
    authorized: bool = False
    source_turn_id: str | None = None
    granted_at: datetime | None = None

    @model_validator(mode="after")
    def validate_grant(self) -> WriteAuthorization:
        if self.authorized and self.granted_at is None:
            raise ValueError("authorized write state requires granted_at.")
        return self


class CustomerConstraints(StrictStateModel):
    """Stable customer-selected constraints stored with canonical identifiers."""

    service_id: str | None = None
    doctor_id: str | None = None
    branch_id: str | None = None
    device_key: str | None = None
    date: DateConstraint | None = None
    time: TimeConstraint | None = None
    time_clock_source: ClockSource = "unspecified"
    package_usage: PackageUsage = "unspecified"
    # Backward-compatibility only. New turns never populate or consume this field.
    pulse_usage: LegacyPulseUsage = "unspecified"

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_clock_source(cls, value: object) -> object:
        """Migrate removed explicit-period authority into the backend-owned bare-clock policy."""
        if not isinstance(value, dict):
            return value
        if value.get("time_clock_source") != "explicit_period":
            return value

        normalized = {**value, "time_clock_source": "bare_spoken_12h"}
        legacy_time = normalized.get("time")
        if not isinstance(legacy_time, dict) or legacy_time.get("mode") != "exact":
            return normalized

        raw_start = legacy_time.get("start_time")
        if not isinstance(raw_start, str):
            return normalized
        try:
            parsed = time.fromisoformat(raw_start.strip())
        except ValueError:
            return normalized

        hour = parsed.hour
        if hour == 0:
            hour = 12
        elif hour > 12:
            hour -= 12
        migrated_time = {
            **legacy_time,
            "start_time": f"{hour:02d}:{parsed.minute:02d}",
            "start_time_ambiguity": "twelve_hour",
        }
        normalized["time"] = migrated_time
        return normalized

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_pulse_usage(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if value.get("pulse_usage") in {"use_existing", "avoid_existing"}:
            return {**value, "pulse_usage": "unspecified"}
        return value


class DerivedBookingState(StrictStateModel):
    """System-derived facts that must be invalidated when their dependencies change."""

    availability_snapshot_id: str | None = None
    selected_slot_ref: str | None = None
    selected_package_id: str | None = None
    package_validated: bool = False
    doctor_compatible: bool | None = None
    device_compatible: bool | None = None
    # Canonical commercial presentation key: service + optional device + path.
    # The legacy device-only field remains readable for persisted pre-contract tasks.
    commercial_basis_presented_key: str | None = None
    commercial_basis_presented_device_key: str | None = None


class OptionChoice(StrictStateModel):
    ref: str
    label: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class OptionSnapshot(StrictStateModel):
    snapshot_id: str
    purpose: ChoicePurpose
    lifecycle_action: LifecycleAction | None = None
    task_version: int
    created_at: datetime
    expires_at: datetime
    options: list[OptionChoice] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot(self) -> OptionSnapshot:
        if self.task_version < 1:
            raise ValueError("task_version must be positive.")
        if self.expires_at <= self.created_at:
            raise ValueError("option snapshot must expire after creation.")
        if not self.options:
            raise ValueError("option snapshot requires at least one option.")
        return self

    def is_current(self, *, task_version: int, now: datetime) -> bool:
        return self.task_version == task_version and now < self.expires_at


class BookingProposal(StrictStateModel):
    proposal_id: str
    patient_id: str
    service_ids: list[str] = Field(default_factory=list)
    main_service_id: str
    additional_service_ids: list[str] = Field(default_factory=list)
    branch_id: str
    doctor_id: str
    start_at: datetime
    end_at: datetime
    device_key: str | None = None
    additional_service_device_keys: dict[str, str] = Field(default_factory=dict)
    package_usage: PackageUsage = "unspecified"
    package_id: str | None = None
    task_version: int = 1
    proposal_version: int = 1
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_proposal(self) -> BookingProposal:
        ordered = list(dict.fromkeys(item for item in self.service_ids if item))
        if not ordered or ordered != self.service_ids:
            raise ValueError("booking proposal services must be unique and non-empty.")
        if self.main_service_id not in ordered:
            raise ValueError("booking proposal main service must be in service_ids.")
        expected_additional = [item for item in ordered if item != self.main_service_id]
        if self.additional_service_ids != expected_additional:
            raise ValueError("booking proposal additional services must match non-main services.")
        if self.end_at <= self.start_at:
            raise ValueError("booking proposal end_at must be after start_at.")
        if self.expires_at <= self.created_at:
            raise ValueError("booking proposal expiry must be after creation.")
        if self.task_version < 1 or self.proposal_version < 1:
            raise ValueError("booking proposal versions must be positive.")
        if set(self.additional_service_device_keys) - set(self.additional_service_ids):
            raise ValueError("booking proposal device map contains a non-additional service.")
        if self.additional_service_device_keys:
            if self.device_key is None:
                raise ValueError("booking proposal additional laser devices require a visit device.")
            if any(
                device_key != self.device_key
                for device_key in self.additional_service_device_keys.values()
            ):
                raise ValueError("booking proposal cannot split laser devices within one visit.")
        return self

    def is_current(self, *, task_version: int, now: datetime) -> bool:
        return self.task_version == task_version and now < self.expires_at


class GroupedBookingState(StrictStateModel):
    """Durable component set for one pending same-visit booking."""

    components: list[CustomerConstraints] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_components(self) -> GroupedBookingState:
        service_ids = [component.service_id for component in self.components]
        if len(service_ids) < 2 or any(service_id is None for service_id in service_ids):
            raise ValueError("grouped booking state requires at least two identified services.")
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("grouped booking state requires distinct service components.")
        return self


class BookingTaskState(StrictStateModel):
    task_type: Literal["booking"] = "booking"
    status: TaskStatus = "collecting"
    write_authorization: WriteAuthorization
    constraints: CustomerConstraints = Field(default_factory=CustomerConstraints)
    requested_service_ids: list[str] = Field(default_factory=list)
    service_device_keys: dict[str, str] = Field(default_factory=dict)
    proposal: BookingProposal | None = None
    grouped: GroupedBookingState | None = None
    derived: DerivedBookingState = Field(default_factory=DerivedBookingState)
    option_snapshot: OptionSnapshot | None = None
    version: int = 1

    @model_validator(mode="after")
    def validate_version(self) -> BookingTaskState:
        if self.version < 1:
            raise ValueError("task version must be positive.")
        if self.write_authorization.operation != "booking":
            raise ValueError("booking task requires booking write authorization type.")
        if len(self.requested_service_ids) != len(set(self.requested_service_ids)):
            raise ValueError("requested booking services must be unique.")
        if self.proposal is not None and self.proposal.service_ids != self.requested_service_ids:
            raise ValueError("booking proposal must match the current requested service set.")
        return self


class RescheduleTarget(StrictStateModel):
    appointment_id: str
    service_id: str | None = None
    doctor_id: str | None = None
    device_key: str | None = None
    start_local: str | None = None
    payment_context: dict[str, object] = Field(default_factory=dict)


class DerivedRescheduleState(StrictStateModel):
    availability_snapshot_id: str | None = None
    selected_slot_ref: str | None = None
    package_validated: bool = False
    doctor_compatible: bool | None = None
    device_compatible: bool | None = None


class RescheduleTaskState(StrictStateModel):
    task_type: Literal["reschedule"] = "reschedule"
    status: TaskStatus = "collecting"
    write_authorization: WriteAuthorization
    target: RescheduleTarget
    replacement: CustomerConstraints
    derived: DerivedRescheduleState = Field(default_factory=DerivedRescheduleState)
    option_snapshot: OptionSnapshot | None = None
    version: int = 1

    @model_validator(mode="after")
    def validate_version(self) -> RescheduleTaskState:
        if self.version < 1:
            raise ValueError("task version must be positive.")
        if self.write_authorization.operation != "reschedule":
            raise ValueError("reschedule task requires reschedule write authorization type.")
        return self


ActiveTaskState = BookingTaskState | RescheduleTaskState
