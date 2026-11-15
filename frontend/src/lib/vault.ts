export type ReceptionVault = {
  branch_id: string;
  branch_name: string;
  activated: boolean;
  currency: string;
  balance_minor: number;
  opening_carried_minor: number;
  cash_today_minor: number;
  refunds_today_minor: number;
  expenses_today_minor: number;
  transfers_today_minor: number;
};

export type MainTreasuryMovement = {
  id: string;
  kind: string;
  amount_minor: number;
  account_kind: string;
  branch_id: string | null;
  created_at: string;
};

export type MainTreasury = {
  currency: string;
  activated: boolean;
  active_channels: string[];
  cash_minor: number;
  visa_minor: number;
  instapay_minor: number;
  total_recorded_minor: number;
  transfers_today_minor: number;
  unattributed_payment_count: number;
  recent_movements: MainTreasuryMovement[];
};


export type VaultPendingPayment = {
  pending_id: string;
  transaction_id: string;
  reason: string;
  amount_minor: number;
  currency: string;
  payment_method: string;
  transaction_type: string;
  source: string;
  external_reference: string | null;
  appointment_id: string | null;
  origin_appointment_id: string | null;
  created_at: string;
  resolution_eligible: boolean;
  verified_branch_id: string | null;
  electronic_action: "opening_review" | "eligible_retry" | null;
  review_outcome: "included_in_opening" | "requires_accounting_decision" | null;
  review_note: string | null;
  reviewed_at: string | null;
  allocations: Array<{
    allocation_id: string;
    appointment_id: string;
    amount_minor: number;
    branch_id: string | null;
    branch_name: string | null;
    patient_matches_source: boolean;
  }>;
};
