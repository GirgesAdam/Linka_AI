import { ActionFeedbackForm } from "@/components/action-feedback-form";
import type { ClinicLaserDevice } from "@/lib/types";

import { saveDoctorLaserWeekday } from "./actions";

export type DoctorLaserWeekday = {
  doctor_id: string;
  doctor_name: string;
  branch_id: string;
  branch_name: string;
  weekday: number;
  excluded_device_keys: string[];
};

const dayNames = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"];

export function DoctorLaserWeekdayPanel({
  rows,
  devices,
}: {
  rows: DoctorLaserWeekday[];
  devices: ClinicLaserDevice[];
}) {
  const activeDevices = devices.filter((device) => device.is_active);
  const doctors = [...new Set(rows.map((row) => row.doctor_id))];
  return (
    <div className="space-y-4">
      {doctors.length === 0 ? (
        <p className="text-sm text-slate-600">
          لا توجد أيام عمل مسجلة حالياً لأطباء الليزر.
        </p>
      ) : activeDevices.length === 0 ? (
        <p className="text-sm text-slate-600">لا توجد أجهزة ليزر نشطة يمكن تخصيصها حالياً.</p>
      ) : doctors.map((doctorId) => {
        const doctorRows = rows.filter((row) => row.doctor_id === doctorId);
        return (
          <div key={doctorId} className="space-y-2 rounded-xl border border-slate-200 p-3">
            {doctorRows.map((row) => {
              const excluded = new Set(row.excluded_device_keys);
              return (
                <ActionFeedbackForm
                  key={`${doctorId}:${row.branch_id}:${row.weekday}`}
                  action={saveDoctorLaserWeekday}
                  refreshAfterSuccess
                  submitLabel="حفظ الأجهزة"
                  className="rounded-lg bg-slate-50 p-3 space-y-2"
                >
                  <input type="hidden" name="doctor_id" value={row.doctor_id} />
                  <input type="hidden" name="branch_id" value={row.branch_id} />
                  <input type="hidden" name="weekday" value={row.weekday} />
                  {activeDevices.map((device) => (
                    <input key={device.device_key} type="hidden" name="device_key" value={device.device_key} />
                  ))}
                  <div className="text-xs font-bold text-slate-700">
                    {row.branch_name} — {dayNames[row.weekday]}
                  </div>
                  <div className="flex flex-wrap gap-x-5 gap-y-2">
                    {activeDevices.map((device) => (
                      <label key={device.device_key} className="flex items-center gap-2 text-sm text-slate-800">
                        <input
                          type="checkbox"
                          name="allowed_device_key"
                          value={device.device_key}
                          defaultChecked={!excluded.has(device.device_key)}
                        />
                        {device.name}
                      </label>
                    ))}
                  </div>
                </ActionFeedbackForm>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
