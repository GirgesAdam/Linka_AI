export type ClinicHour = { weekday: number; start_time: string; end_time: string };
export type VisitingWindow = { start_at: string; end_at: string };

export type ClinicServiceV2 = {
  id: string;
  name: string;
  category: string | null;
  duration_minutes: number;
  price: string | number;
};

export type ClinicDoctorV2 = {
  id: string;
  staff_id: string;
  full_name: string;
  doctor_type: "regular" | "visiting";
  specialization: string | null;
  service_ids: string[];
  weekly_hours: ClinicHour[];
  visiting_windows: VisitingWindow[];
};

export type ClinicSetupV2Snapshot = {
  workspace_id: string;
  clinic: {
    branch_id: string | null;
    name: string;
    phone: string | null;
    address: string | null;
    city: string | null;
    timezone: "Africa/Cairo" | string;
  };
  services: ClinicServiceV2[];
  doctors: ClinicDoctorV2[];
  clinic_hours: ClinicHour[];
  booking_policy: {
    slot_interval_minutes: number;
    minimum_notice_minutes: number;
    booking_horizon_days: number;
    cancellation_notice_minutes: number;
    allow_same_day_booking: boolean;
    require_confirmation: boolean;
  };
  readiness: {
    ready: boolean;
    checks: Record<string, boolean>;
    missing: string[];
    progress_percent: number;
  };
};

export type HistoricalImportMode = "append" | "replace_previous_imports";
export type HistoricalBatch = {
  batch_id: string;
  mode: HistoricalImportMode;
  status: "preview_ready" | "importing" | "imported" | "failed";
  schema_version: string;
  source_name: string;
  summary: Record<string, unknown>;
  error_message: string | null;
};

export type HistoricalPreview = {
  batch: HistoricalBatch;
  ready_counts: Record<string, number>;
  rejected_counts: Record<string, number>;
  issue_groups: Array<{
    code: string;
    message: string;
    entity_type: "patient" | "appointment" | "payment" | "payment_allocation" | "package";
    occurrence_count: number;
    example_rows: number[];
  }>;
  can_import: boolean;
  projected_counts: {
    patients_to_create?: number;
    existing_patients_reused?: number;
    packages_to_create?: number;
    historical_payments_to_create?: number;
  };
  total_amount_imported_minor: number;
};

export type ClinicSetupImportIssue = {
  sheet: string;
  row: number;
  message: string;
};

export type ClinicSetupImportResult = {
  imported_counts: Record<string, number>;
  skipped_counts: Record<string, number>;
  issues: ClinicSetupImportIssue[];
  snapshot: ClinicSetupV2Snapshot;
};

export type ClinicSetupDraftValue = string | number | boolean | null;
export type ClinicSetupDraftRow = Record<string, ClinicSetupDraftValue>;
export type ClinicSetupDraft = {
  clinic_profile: ClinicSetupDraftRow;
  services: ClinicSetupDraftRow[];
  doctors: ClinicSetupDraftRow[];
  doctor_services: ClinicSetupDraftRow[];
  clinic_hours: ClinicSetupDraftRow[];
  doctor_hours: ClinicSetupDraftRow[];
  visiting_windows: ClinicSetupDraftRow[];
  booking_policy: ClinicSetupDraftRow;
};

export type ClinicSetupPreviewResult = {
  draft: ClinicSetupDraft;
  issues: ClinicSetupImportIssue[];
  recognized_sheets: string[];
};
