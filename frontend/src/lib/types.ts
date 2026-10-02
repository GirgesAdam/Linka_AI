export type WorkspaceRole = "admin" | "member";

export interface CurrentUser {
  id: string;
  auth_user_id: string;
  email: string;
  full_name: string | null;
}
export interface WorkspaceAccess {
  workspace_id: string;
  workspace_name: string;
  workspace_slug: string;
  role: WorkspaceRole;
}
export interface MeResponse { user: CurrentUser; workspaces: WorkspaceAccess[]; }

export interface DashboardSummary {
  active_patients: number;
  appointments_today: number;
  upcoming_appointments: number;
  appointments_after_today: number;
  open_handoffs: number;
  active_channels: number;
  failed_automation_jobs: number;
  recent_appointments: DashboardAppointment[];
  today_appointments: DashboardAppointment[];
  next_appointments: DashboardAppointment[];
}
export interface DashboardAppointment {
  id: string; patient_id: string; patient_name: string; service_name: string;
  branch_name: string; doctor_name: string; status: string; start_at: string;
  end_at: string; price_minor: number; currency: string;
}

export interface HandoffQueueItem {
  id: string; workspace_id: string; conversation_id: string; patient_id: string;
  status: "pending" | "claimed" | "resolved";
  category: string; priority: "low" | "normal" | "high" | "urgent";
  source: string; reason: string; context_json: Record<string, unknown>; assigned_user_id: string | null;
  created_by_user_id: string | null; claimed_at: string | null; resolved_at: string | null;
  resolved_by_user_id: string | null; resolution_note: string | null;
  created_at: string; updated_at: string; patient_name: string; patient_phone: string | null;
  channel: string; conversation_last_message_at: string | null;
  conversation_owner_type: "ai" | "human"; conversation_unread_count: number;
  assigned_user_name: string | null; assigned_user_email: string | null;
}

export interface InboxAssignee {
  id: string; full_name: string | null; email: string;
}
export interface InboxConversationListItem {
  id: string; workspace_id: string; patient_id: string; channel: string; status: string;
  owner_type: "ai" | "human"; unread_count: number; assigned_user_id: string | null;
  assigned_user: InboxAssignee | null; subject: string | null; started_at: string;
  last_message_at: string | null;
  patient: { id: string; first_name: string; last_name: string | null; phone: string | null };
  active_handoff: HandoffRead | null; last_message: InboxMessage | null;
}

export interface InboxMessage {
  id: string; conversation_id: string; channel_connection_id: string | null;
  sender_type: string; direction: string; message_type: string; content: string | null;
  delivery_status: string; sent_by_user_id: string | null; metadata_json: Record<string, unknown>;
  created_at: string;
}
export interface HandoffRead {
  id: string; workspace_id: string; conversation_id: string; patient_id: string; status: string;
  category: string; priority: string; source: string; reason: string; context_json: Record<string, unknown>; assigned_user_id: string | null;
  claimed_at: string | null; resolved_at: string | null; resolution_note: string | null;
  created_at: string; updated_at: string;
}
export interface InboxConversation {
  id: string; workspace_id: string; patient_id: string; channel: string; channel_connection_id: string | null;
  status: string; assigned_user_id: string | null; owner_type: "ai" | "human"; unread_count: number;
  ownership_changed_at: string; subject: string | null; started_at: string;
  last_message_at: string | null; closed_at: string | null;
  patient: { id: string; first_name: string; last_name: string | null; phone: string | null };
  assigned_user: InboxAssignee | null;
  active_handoff: HandoffRead | null; handoff_history: HandoffRead[]; messages: InboxMessage[];
  handoff_events: Array<{id:string; event_type:string; actor_type:string; actor_user_id:string|null; metadata_json:Record<string,unknown>; created_at:string}>;
}

export interface Patient {
  id: string; workspace_id: string; first_name: string; last_name: string | null; phone: string | null;
  gender: string | null; birth_date: string | null; preferred_language: string;
  preferred_branch_id: string | null; source: string; source_detail: string | null; status: string;
  marketing_consent: boolean; marketing_consent_at: string | null; whatsapp_opt_in: boolean; whatsapp_opt_in_at: string | null; whatsapp_opt_in_source: string | null; last_contact_at: string | null;
  created_at: string; updated_at: string;
}
export interface PatientTag {
  id: string; workspace_id: string; name: string; color: string | null; is_active: boolean;
}
export interface PatientNote {
  id: string; workspace_id: string; patient_id: string; author_user_id: string | null;
  note_type: "general" | "preference" | "customer_service" | "follow_up";
  content: string; is_pinned: boolean; created_at: string; updated_at: string;
}
export interface PatientCRMStats {
  total_appointments: number; completed_appointments: number; no_show_appointments: number;
  upcoming_appointments: number; total_conversations: number; open_conversations: number;
  active_handoffs: number; active_leads: number; open_tasks: number; overdue_tasks: number;
  next_task_at: string | null; next_appointment_at: string | null; last_appointment_at: string | null;
}
export interface PatientTimelineAppointment {
  id: string; status: string; start_at: string; end_at: string; service_name: string; branch_name: string;
  doctor_name: string; price_minor: number; currency: string; from_status: string | null;
  to_status: string | null; reason: string | null;
}
export interface PatientTimelineMessage {
  id: string; conversation_id: string; sender_type: string; direction: string; message_type: string; content: string | null;
  delivery_status: string; channel: string;
}
export interface PatientTimelineHandoff {
  id: string; conversation_id: string; event_type: string; status: string; category: string; priority: string; reason: string;
}
export interface PatientTimelineNote {
  id: string; note_type: PatientNote["note_type"]; content: string; is_pinned: boolean;
}
export interface PatientTimelineTask {
  id:string; event_type:"created"|"completed"; status:CRMTask["status"]; priority:CRMTask["priority"];
  task_type:CRMTask["task_type"]; title:string; due_at:string; assigned_user_id:string|null;
}
export interface PatientTimelinePayment {
  id:string; appointment_id:string|null; transaction_type:"payment"|"refund"; amount_minor:number; currency:string;
  payment_method:string; reference_transaction_id:string|null; reason:string|null;
}
export type PatientTimelineKind = "patient_created" | "note" | "appointment" | "appointment_status" | "message" | "handoff" | "task" | "payment";
export interface PatientTimelineEvent {
  id: string; kind: PatientTimelineKind; occurred_at: string; actor_type: string | null;
  actor_user_id: string | null; actor_name: string | null; appointment: PatientTimelineAppointment | null;
  message: PatientTimelineMessage | null; handoff: PatientTimelineHandoff | null; note: PatientTimelineNote | null; task: PatientTimelineTask | null;
  payment: PatientTimelinePayment | null;
}
export interface PatientProfile {
  patient: Patient; stats: PatientCRMStats; tags: PatientTag[]; notes: PatientNote[];
  timeline: PatientTimelineEvent[]; latest_conversation_id: string | null;
}

export interface CRMTask {
  id:string; workspace_id:string; patient_id:string; lead_id:string|null; conversation_id:string|null;
  assigned_user_id:string|null; created_by_user_id:string|null; completed_by_user_id:string|null;
  task_type:"follow_up"|"general"; source:"manual"|"ai"|"system"; execution_mode:"human"|"ai";
  status:"pending"|"in_progress"|"completed"|"cancelled"; priority:"low"|"normal"|"high"|"urgent";
  title:string; description:string|null; due_at:string; completed_at:string|null; patient_name:string;
  assigned_user_name:string|null; assigned_user_email:string|null; is_overdue:boolean; created_at:string; updated_at:string;
}

export type AppointmentStatus = "pending" | "confirmed" | "checked_in" | "in_progress" | "completed" | "cancelled" | "no_show" | "rescheduled";
export type AppointmentOperationAction = "confirm" | "reschedule" | "cancel" | "complete" | "no_show";
export interface Appointment {
  id:string; workspace_id:string; patient_id:string; branch_id:string; doctor_id:string; service_id:string; patient_package_id:string|null; lead_id:string|null;
  created_by_user_id:string|null; rescheduled_from_appointment_id:string|null; status:AppointmentStatus; source:string;
  start_at:string; end_at:string; busy_start_at:string; busy_end_at:string; duration_minutes:number; price_minor:number; currency:string;
  doctor_assignment_known?:boolean; is_quick_booking?:boolean; laser_device_key?:string|null; laser_device_name?:string|null; laser_pulses_used?:number|null; discount_minor?:number;
  payment_status:string; amount_paid_minor:number|null; payment_method:string; billing_context:string; package_external_id:string|null; customer_note:string|null; cancellation_reason:string|null;
  confirmed_at:string|null; cancelled_at:string|null; completed_at:string|null; no_show_at:string|null; created_at:string; updated_at:string;
}
export interface AppointmentStatusHistory {
  id:string; workspace_id:string; appointment_id:string; changed_by_user_id:string|null; from_status:AppointmentStatus|null;
  to_status:AppointmentStatus; reason:string|null; metadata:Record<string,unknown>; created_at:string;
}
export interface AppointmentAutomation {
  id:string; rule_key:string; rule_name:string; status:string; scheduled_for:string; attempts:number; last_error:string|null;
}
export interface AppointmentOperationsDetail {
  appointment:Appointment; patient:{id:string; name:string; phone:string|null}; branch:{id:string; name:string}; service:{id:string; name:string};
  doctor:{id:string; name:string}; timezone:string; history:AppointmentStatusHistory[]; automations:AppointmentAutomation[];
  allowed_actions:AppointmentOperationAction[]; cancellation_override_required:boolean; can_override_cancellation_policy:boolean;
}
export interface PaymentTransaction {
  id:string; workspace_id:string; appointment_id:string|null; origin_appointment_id:string|null; patient_id:string; created_by_user_id:string|null;
  reference_transaction_id:string|null; patient_package_id:string|null; patient_pulse_pack_id:string|null; transaction_type:"payment"|"refund"; amount_minor:number; allocated_amount_minor:number|null; currency:string; payment_method:string;
  source:string; external_reference:string|null; reason:string|null; created_at:string; refunded_minor:number; refundable_minor:number;
}
export interface AppointmentPaymentSummary {
  appointment_id:string; patient_id:string; currency:string; price_minor:number; subtotal_minor:number; discount_minor:number; service_price_minor:number; products_total_minor:number; additional_services_total_minor:number; package_sales_total_minor:number; pulse_pack_sales_total_minor:number; pulse_overage_total_minor:number; gross_paid_minor:number; refunded_minor:number;
  net_paid_minor:number; balance_minor:number; payment_status:string; billing_context:string; package_external_id:string|null; transactions:PaymentTransaction[]; can_refund:boolean;
}
export interface PatientPackage {
  id:string; workspace_id:string; patient_id:string; service_id:string; purchase_transaction_id:string|null; external_id:string|null;
  name:string; sessions_purchased:number; sessions_reserved:number; sessions_consumed:number; sessions_remaining:number;
  sale_price_minor:number; standalone_session_price_minor_at_purchase:number|null; currency:string; purchased_at:string; expires_at:string|null; status:string; effective_status:string; source:string;
  created_at:string; updated_at:string;
}
export interface ClinicLaserDevice {
  id:string; workspace_id:string; device_key:string; name:string; is_active:boolean; created_at:string; updated_at:string;
}
export interface PulseBillingSettings {
  device_key:string; device_name:string; overage_price_minor:number|null; currency:string;
}
export interface PulsePackOffer {
  id:string; workspace_id:string; device_key:string; device_name:string;
  pulses_count:number; price_minor:number; currency:string; is_active:boolean; created_at:string; updated_at:string;
}
export interface PatientPulsePack {
  id:string; workspace_id:string; patient_id:string; pulse_pack_offer_id:string|null; origin_appointment_id:string|null; purchase_transaction_id:string|null;
  device_key:string; device_name:string; pulses_purchased:number; pulses_consumed:number; pulses_remaining:number;
  sale_price_minor:number; amount_paid_minor:number; amount_refunded_minor:number; balance_due_minor:number;
  standalone_pulse_price_minor_at_purchase:number|null; currency:string; purchased_at:string; expires_at:string|null; status:string; effective_status:string;
  created_at:string; updated_at:string;
}
export interface PulseBalance {
  device_key:string; device_name:string; pulses_purchased:number; pulses_consumed:number; pulses_remaining:number; active_pack_count:number;
}
export interface AppointmentPulseSettlement {
  appointment_id:string; pulses_used:number; pulses_from_balance:number; deficit_pulses:number;
  resolution:"pending"|"balance"|"new_pack"|"overage"; resolution_pulse_pack_id:string|null;
  overage_unit_price_minor:number|null; overage_charge_minor:number; resolved_at:string|null;
  available_balance_after:number; currency:string;
}

export interface AvailabilitySlot {
  branch_id:string; doctor_id:string; service_id:string; start_at:string; end_at:string; price_minor:number; currency:string;
}
export interface AvailabilityResponse { date:string; timezone:string; slots:AvailabilitySlot[]; }
export interface Branch { id:string; name:string; city:string|null; is_active:boolean; }
export interface Service { id:string; name:string; category:string|null; operational_category:"laser"|"dermatology"|"slimming"; duration_minutes:number; price_minor:number; currency:string; requires_laser_device?:boolean; is_active:boolean; }
export interface Doctor { id:string; staff_id:string; specialization:string|null; is_active:boolean; }
export interface Staff { id:string; first_name:string; last_name:string; is_active:boolean; }

export interface AutomationRule {
  id:string; key:string; name:string; enabled:boolean; trigger_kind:string; offset_minutes:number;
  channel:string; template_name:string; template_language:string; max_lateness_minutes:number;
  config_json:Record<string,unknown>; created_at:string; updated_at:string;
}
export interface AutomationJob {
  id:string; rule_id:string|null; appointment_id:string|null; crm_task_id:string|null; patient_id:string; job_kind:"appointment_rule"|"crm_follow_up"; status:string; scheduled_for:string;
  attempts:number; next_attempt_at:string|null; last_error:string|null; completed_at:string|null; created_at:string;
  dispatch_status:string|null; dispatch_last_error:string|null; attention_reason:"execution_failed"|"delivery_failed"|"stuck_processing"|null;
}
export interface AutomationOperationsOverview {
  now:string; enabled_rules:number; queued_jobs:number; due_jobs:number; processing_jobs:number; failed_jobs:number; delivery_failed_jobs:number;
  attention_count:number; next_job_at:string|null; worker_state:"healthy"|"stale"|"missing"|"not_required"; worker_last_seen_at:string|null; worker_fresh_within_minutes:number;
}
export interface ChannelConnection {
  id:string; channel:string; provider:string; display_name:string; status:string; external_account_id:string|null;
  config_json:Record<string,unknown>; created_at:string; updated_at:string;
}
export interface WorkspaceMember {
  membership_id:string; user_id:string; auth_user_id:string|null; email:string; full_name:string|null;
  role:WorkspaceRole; is_active:boolean;
}

export interface AnalyticsMoney {
  currency:string; completed_value_minor:number; gross_paid_minor:number; refunded_minor:number; recorded_paid_minor:number; outstanding_balance_minor:number;
}
export interface AnalyticsBreakdown {
  id:string; name:string; appointments:number; completed:number; no_show:number; cancelled:number;
}
export interface AnalyticsDaily {
  date:string; appointments:number; completed:number; no_show:number; cancelled:number; new_patients:number;
}
export interface AnalyticsOverview {
  days:number; timezone:string; start_at:string; end_at:string; total_appointments:number; completed_appointments:number;
  no_show_appointments:number; cancelled_appointments:number; pending_or_confirmed_appointments:number;
  attendance_rate_percent:number; no_show_rate_percent:number; cancellation_rate_percent:number; new_patients:number;
  conversations_started:number; handoffs_created:number; money:AnalyticsMoney[]; top_services:AnalyticsBreakdown[];
  top_branches:AnalyticsBreakdown[]; daily:AnalyticsDaily[];
}

export interface HistoricalAnalyticsMoney {
  currency:string; gross_paid_minor:number; refunded_minor:number; net_paid_minor:number;
}
export interface HistoricalAnalyticsService {
  service_id:string; service_name:string; completed_appointments:number; unique_patients:number;
}
export interface HistoricalAnalytics {
  data_start_at:string|null; data_end_at:string|null; total_patients:number; repeat_patients:number; repeat_patient_rate_percent:number;
  total_appointments:number; completed_appointments:number; money:HistoricalAnalyticsMoney[]; top_services:HistoricalAnalyticsService[];
}

export interface ActivityEvent {
  id:string; action:string; actor_type:"staff"|"ai"|"system"; actor_user_id:string|null; actor_label:string;
  entity_type:string; entity_id:string|null; summary:string; metadata:Record<string,unknown>; created_at:string;
}

export type AnalyticsBIOperation =
  | "clinic_summary"
  | "revenue_trend"
  | "appointment_outcomes"
  | "service_performance"
  | "service_retention"
  | "doctor_performance"
  | "branch_performance"
  | "top_repeat_patients"
  | "top_value_patients"
  | "lapsed_patients"
  | "new_patients_trend"
  | "patient_history_lookup";

export interface AnalyticsBIPlan {
  operation: AnalyticsBIOperation;
  lookback_days: number | null;
  inactivity_days: number | null;
  limit: number;
  service_ids: string[];
  branch_ids: string[];
  doctor_ids: string[];
  currency: string | null;
  patient_name: string | null;
  patient_phone: string | null;
  reason: string;
}
export interface AnalyticsBIMetric {
  key: string;
  label: string;
  value: number | string;
  currency: string | null;
}
export interface AnalyticsBIResultRow {
  key: string | null;
  label: string;
  secondary_label: string | null;
  metrics: AnalyticsBIMetric[];
}
export interface AnalyticsBIAnswer {
  question: string;
  plan: AnalyticsBIPlan;
  period_label: string;
  answer: string;
  definitions: string[];
  rows: AnalyticsBIResultRow[];
  model: string | null;
}



export type AnalyticsBusinessMetric =
  | "appointments" | "completed_appointments" | "no_show_appointments" | "cancelled_appointments"
  | "unique_patients" | "attendance_rate" | "no_show_rate" | "cancellation_rate"
  | "gross_paid_minor" | "refunded_minor" | "net_paid_minor" | "avg_net_paid_per_paying_patient_minor" | "paying_patients"
  | "paid_completed_appointments" | "completion_rate" | "paid_completion_rate" | "booking_to_paid_rate"
  | "repeat_patients" | "repeat_rate" | "new_patients" | "same_service_repeat_rate";
export type AnalyticsBusinessDimension = "service" | "branch" | "doctor" | "source" | "day" | "week" | "month";
export interface AnalyticsBusinessPlan {
  kind:"business_analytics"; metrics:AnalyticsBusinessMetric[]; group_by:AnalyticsBusinessDimension[];
  lookback_days:number|null; start_date:string|null; end_date:string|null; comparison:"none"|"previous_period";
  service_ids:string[]; branch_ids:string[]; doctor_ids:string[]; currency:string|null; limit:number;
  sort_metric:AnalyticsBusinessMetric|null; sort_direction:"asc"|"desc"; reason:string;
}

export type AnalyticsAudienceSort =
  | "last_activity_desc"
  | "last_activity_asc"
  | "matching_visits_desc"
  | "net_paid_desc"
  | "first_seen_desc";
export interface AnalyticsAudiencePlan {
  kind:"patient_audience"; lookback_days:number|null; inactivity_days:number|null; limit:number;
  service_ids:string[]; branch_ids:string[]; doctor_ids:string[];
  appointment_statuses:Array<"pending"|"confirmed"|"checked_in"|"in_progress"|"completed"|"cancelled"|"no_show">;
  min_matching_visits:number; max_matching_visits:number|null; has_future_appointment:boolean|null;
  marketing_consent:boolean|null; patient_statuses:Array<"active"|"inactive"|"blocked">;
  min_net_paid_minor:number|null; max_net_paid_minor:number|null; currency:string|null;
  sort_by:AnalyticsAudienceSort; reason:string;
}
export type AnalyticsActionKind = "none"|"save_audience"|"follow_up_tasks"|"whatsapp_campaign";
export interface AnalyticsActionProposal {
  kind:AnalyticsActionKind; title:string|null; description:string|null; due_in_days:number|null;
  priority:"low"|"normal"|"high"|"urgent"|null; reason:string;
}
export interface AnalyticsComposeAnswer {
  question:string; mode:"metric"|"business"|"audience"; metric_plan:AnalyticsBIPlan|null; business_plan:AnalyticsBusinessPlan|null; audience_plan:AnalyticsAudiencePlan|null;
  action:AnalyticsActionProposal; period_label:string; answer:string; definitions:string[]; rows:AnalyticsBIResultRow[]; model:string|null;
}


export type AnalyticsCatalogCategory = "revenue"|"patients"|"appointments"|"services"|"doctors"|"branches"|"retention"|"funnels";
export type AnalyticsCatalogResultKind = "summary"|"trend"|"breakdown"|"patient_list"|"funnel";
export type AnalyticsCatalogChart = "kpi"|"line"|"bar"|"heatmap"|"funnel"|"table";
export type AnalyticsCatalogFilter =
  | "period" | "service" | "branch" | "doctor" | "comparison" | "granularity" | "limit"
  | "inactivity_days" | "min_visits" | "max_visits" | "future_booking" | "marketing_consent";
export type AnalyticsCatalogAction = "export"|"save_patient_group"|"follow_up_tasks"|"whatsapp_campaign";
export interface AnalyticsCatalogEntityOption { id:string; name:string; }
export interface AnalyticsCatalogDefinition {
  key:string; category:AnalyticsCatalogCategory; title:string; description:string; result_kind:AnalyticsCatalogResultKind;
  default_chart:AnalyticsCatalogChart; supported_charts:AnalyticsCatalogChart[]; filters:AnalyticsCatalogFilter[];
  allowed_actions:AnalyticsCatalogAction[]; default_lookback_days:number|null; default_granularity:"day"|"week"|"month"|null;
  default_inactivity_days:number|null; default_limit:number; default_min_visits:number|null; default_max_visits:number|null; chart_metric_keys:string[];
}
export interface AnalyticsCatalog {
  analyses:AnalyticsCatalogDefinition[]; services:AnalyticsCatalogEntityOption[]; branches:AnalyticsCatalogEntityOption[];
  doctors:AnalyticsCatalogEntityOption[];
}
export interface AnalyticsCatalogRunRequest {
  analysis_key:string; lookback_days:number|null; all_history:boolean; start_date:string|null; end_date:string|null;
  service_ids:string[]; branch_ids:string[]; doctor_ids:string[]; comparison:boolean; granularity:"day"|"week"|"month"|null;
  limit:number|null; inactivity_days:number|null; min_visits:number|null; max_visits:number|null;
  has_future_appointment:boolean|null; marketing_consent:boolean|null;
}
export type AnalyticsSavedViewDisplayMode = "visual"|"table"|"both";
export interface AnalyticsSavedView {
  id:string; workspace_id:string; created_by_user_id:string|null; name:string; analysis_key:string; request:AnalyticsCatalogRunRequest;
  chart:AnalyticsCatalogChart|null; display_mode:AnalyticsSavedViewDisplayMode; created_at:string; updated_at:string;
}
export type AnalyticsCatalogSeriesFormat = "number"|"percent"|"money";
export interface AnalyticsCatalogChartSeries { key:string; label:string; format:AnalyticsCatalogSeriesFormat; currency:string|null; values:Array<number|null>; }
export interface AnalyticsCatalogChartData { labels:string[]; series:AnalyticsCatalogChartSeries[]; }
export interface AnalyticsCatalogRun {
  request:AnalyticsCatalogRunRequest; analysis_key:string; title:string; category:AnalyticsCatalogCategory; result_kind:AnalyticsCatalogResultKind; chart:AnalyticsCatalogChart;
  supported_charts:AnalyticsCatalogChart[]; chart_metric_keys:string[]; chart_data:AnalyticsCatalogChartData; highlights:AnalyticsBIMetric[]; allowed_actions:AnalyticsCatalogAction[]; period_label:string;
  answer:string; definitions:string[]; rows:AnalyticsBIResultRow[]; business_plan:AnalyticsBusinessPlan|null; audience_plan:AnalyticsAudiencePlan|null;
}

export interface CRMCohortMember {
  patient_id: string;
  rank: number;
  patient_name: string;
  patient_phone: string | null;
  snapshot_metrics: Array<Record<string, unknown>>;
}
export interface CRMCohort {
  id: string;
  workspace_id: string;
  created_by_user_id: string | null;
  name: string;
  request_id: string;
  source: "analytics_bi";
  status: "active" | "archived";
  analytics_operation: string;
  question: string;
  plan: AnalyticsBIPlan | AnalyticsAudiencePlan;
  period_label: string;
  member_count: number;
  created_at: string;
  updated_at: string;
  members: CRMCohortMember[];
}
export interface CohortFollowUpResult {
  cohort_id: string;
  request_id: string;
  member_count: number;
  created_tasks: number;
  reused_tasks: number;
  task_ids: string[];
}

export type CRMCampaignRecipientStatus =
  | "eligible" | "skipped_no_consent" | "skipped_inactive" | "skipped_no_route"
  | "cancelled_no_consent" | "cancelled_inactive" | "cancelled_no_route"
  | "queued" | "processing" | "sent" | "delivered" | "read" | "failed" | "cancelled";
export interface CRMCampaignRecipient {
  id:string; patient_id:string; rank:number; patient_name:string; patient_phone:string|null;
  status:CRMCampaignRecipientStatus; reason:string|null; message_id:string|null; dispatch_id:string|null; scheduled_at:string|null;
}
export interface CRMCampaign {
  id:string; workspace_id:string; cohort_id:string; channel_connection_id:string; created_by_user_id:string|null; confirmed_by_user_id:string|null;
  request_id:string; confirmation_id:string|null; name:string; status:"draft"|"confirmed"|"cancelled";
  template_name:string; template_language:string; body_parameter_keys:Array<"patient_first_name"|"clinic_name"|"cohort_name">;
  rate_limit_per_minute:number; recipient_count:number; eligible_count:number; confirmed_at:string|null; created_at:string; updated_at:string;
  recipients:CRMCampaignRecipient[];
}

export interface CampaignAnalyticsMetrics {
  recipient_count:number; eligible_count:number; dispatch_count:number; sent_count:number; delivered_count:number; read_count:number;
  failed_count:number; cancelled_count:number; delivery_rate:number; read_rate:number; tracked_booking_count:number; completed_booking_count:number;
  booking_conversion_rate:number; attributed_revenue_minor:number; currency:string;
}
export interface CampaignAnalyticsCampaign extends CampaignAnalyticsMetrics {
  campaign_id:string; cohort_id:string; name:string; status:string; template_name:string; confirmed_at:string|null;
}
export interface CampaignAnalyticsOverview {
  period_label:string; attribution_window_days:number; historical_booking_backfill:boolean; totals:CampaignAnalyticsMetrics;
  campaigns:CampaignAnalyticsCampaign[]; definitions:string[];
}

export interface CohortCampaignConfirmResult {
  campaign_id:string; confirmation_id:string; recipient_count:number; preview_eligible_count:number;
  queued_count:number; cancelled_before_queue:number; status:"confirmed";
}

export interface AnalyticsAudienceActionResult {
  audience:CRMCohort; action_kind:"save_audience"|"follow_up_tasks"|"whatsapp_campaign";
  follow_up:CohortFollowUpResult|null; next_step:"saved"|"tasks_created"|"campaign_setup";
}
