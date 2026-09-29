from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.meta_whatsapp_templates import STANDARD_TEMPLATES_BY_RULE_KEY


class WhatsAppDirectConnect(BaseModel):
    app_id: str = Field(min_length=1, max_length=64)
    waba_id: str = Field(min_length=1, max_length=64)
    phone_number_id: str = Field(min_length=1, max_length=64)
    access_token: str = Field(min_length=16, max_length=8192)
    app_secret: str = Field(min_length=8, max_length=512)

    @field_validator("app_id", "waba_id", "phone_number_id")
    @classmethod
    def validate_meta_id(cls, value: str) -> str:
        clean = value.strip()
        if not clean.isdigit():
            raise ValueError("Meta identifiers must contain digits only.")
        return clean

    @field_validator("access_token", "app_secret")
    @classmethod
    def clean_secret(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Meta credential cannot be empty.")
        return clean


class WhatsAppTemplateSetupStatus(BaseModel):
    rule_key: str
    label: str
    name: str
    language: str
    category: str
    status: str
    error: str | None = None


class WhatsAppSetupState(BaseModel):
    connection_id: UUID | None = None
    connection_status: Literal["active", "paused", "disconnected"] | None = None
    connected: bool = False
    display_name: str | None = None
    display_phone_number: str | None = None
    verified_name: str | None = None
    provider_health_state: str | None = None
    provider_error_code: str | None = None
    provider_error: str | None = None
    direct_setup_available: bool = False
    provider_credentials_ready: bool = False
    transport_ready: bool = False
    templates_ready: bool = False
    ready_for_automations: bool = False
    meta_app_id: str | None = None
    webhook_callback_url: str | None = None
    webhook_verify_token: str | None = None
    webhook_verified: bool = False
    templates: list[WhatsAppTemplateSetupStatus] = Field(default_factory=list)
    admin_action: Literal[
        "connect_meta_direct",
        "configure_webhook",
        "resolve_meta_restriction",
        "wait_for_template_review",
        "none",
    ] = "none"
    admin_message: str | None = None
    system_message: str | None = None

    @model_validator(mode="after")
    def derive_operational_template_readiness(self) -> Self:
        required_rule_keys = set(STANDARD_TEMPLATES_BY_RULE_KEY)
        approved_rule_keys = {
            item.rule_key for item in self.templates if item.status.lower() == "approved"
        }
        self.templates_ready = bool(required_rule_keys) and required_rule_keys.issubset(
            approved_rule_keys
        )
        self.ready_for_automations = bool(
            self.connected
            and self.connection_status == "active"
            and self.provider_credentials_ready
            and self.webhook_verified
            and self.transport_ready
            and self.templates_ready
            and self.provider_health_state not in {"disabled", "degraded"}
        )

        if self.templates_ready:
            if self.admin_action == "wait_for_template_review":
                self.admin_action = "none"
                self.admin_message = None
            if (
                self.transport_ready
                and self.system_message
                and "القوالب" in self.system_message
            ):
                self.system_message = None
            return self

        if self.admin_action == "wait_for_template_review":
            missing_rule_keys = required_rule_keys - approved_rule_keys
            missing_group_has_rejection = any(
                item.rule_key in missing_rule_keys and item.status.lower() == "rejected"
                for item in self.templates
            )
            if not missing_group_has_rejection:
                self.admin_action = "none"
                self.admin_message = None
                if self.webhook_verified and self.transport_ready:
                    self.system_message = (
                        "Linka أنشأت القوالب تلقائيًا وبتتابع اعتماد قالب واحد على الأقل لكل Automation."
                    )
        return self


class WhatsAppDirectConnectResult(BaseModel):
    state: WhatsAppSetupState
