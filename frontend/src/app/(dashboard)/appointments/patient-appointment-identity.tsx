export type AppointmentPatientDisplay = {
  name: string;
  phone: string | null;
};

/** Shared name + phone display for every appointment card (without nested links). */
export function PatientAppointmentIdentity({ patient }: { patient?: AppointmentPatientDisplay }) {
  const phone = patient?.phone?.trim();

  return (
    <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm font-black text-slate-950">
      <span dir="rtl" className="min-w-0 break-words">
        {patient?.name || "عميل"}
      </span>
      {phone ? (
        <bdi dir="ltr" className="shrink-0 whitespace-nowrap text-xs font-semibold text-slate-600">
          {phone}
        </bdi>
      ) : null}
    </span>
  );
}
