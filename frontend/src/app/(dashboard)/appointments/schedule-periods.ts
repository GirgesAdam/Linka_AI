export type ScheduleHour = {
  start_time: string;
  end_time: string;
};

export type ScheduleAppointment = {
  start_at: string;
  end_at: string;
};

export type ScheduleAvailabilityBlock = {
  start_at: string;
  end_at: string;
  scope: string;
};

export type SchedulePeriod<
  TAppointment extends ScheduleAppointment = ScheduleAppointment,
  TBlock extends ScheduleAvailabilityBlock = ScheduleAvailabilityBlock,
> = {
  start: number;
  end: number;
  appointments: TAppointment[];
  block: TBlock | null;
};

function toMinutes(value: string) {
  const [hour, minute] = value.slice(0, 5).split(":").map(Number);
  return hour * 60 + minute;
}

function minuteInTimezone(value: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return Number(values.hour) * 60 + Number(values.minute);
}

export function buildSchedulePeriods<
  TAppointment extends ScheduleAppointment,
  TBlock extends ScheduleAvailabilityBlock,
>(
  appointments: TAppointment[],
  interval: ScheduleHour,
  timezone: string,
  blocks: TBlock[] = [],
): SchedulePeriod<TAppointment, TBlock>[] {
  const workStart = toMinutes(interval.start_time);
  const workEnd = toMinutes(interval.end_time);
  const bookings = appointments
    .map((appointment) => {
      const start = minuteInTimezone(appointment.start_at, timezone);
      let end = minuteInTimezone(appointment.end_at, timezone);
      if (end <= start) end += 24 * 60;
      return {
        appointment,
        start: Math.max(start, workStart),
        end: Math.min(end, workEnd),
      };
    })
    .filter((booking) => booking.end > booking.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const basePeriods: SchedulePeriod<TAppointment, TBlock>[] = [];
  let cursor = workStart;
  let index = 0;
  while (index < bookings.length) {
    const first = bookings[index];
    const busyStart = first.start;
    let busyEnd = first.end;
    const busyAppointments = [first.appointment];
    index += 1;
    while (index < bookings.length && bookings[index].start < busyEnd) {
      busyEnd = Math.max(busyEnd, bookings[index].end);
      busyAppointments.push(bookings[index].appointment);
      index += 1;
    }
    if (busyStart > cursor) {
      basePeriods.push({ start: cursor, end: busyStart, appointments: [], block: null });
    }
    basePeriods.push({ start: busyStart, end: busyEnd, appointments: busyAppointments, block: null });
    cursor = Math.max(cursor, busyEnd);
  }
  if (cursor < workEnd) {
    basePeriods.push({ start: cursor, end: workEnd, appointments: [], block: null });
  }

  const blockRanges = blocks
    .map((block) => ({
      block,
      start: Math.max(minuteInTimezone(block.start_at, timezone), workStart),
      end: Math.min(minuteInTimezone(block.end_at, timezone), workEnd),
    }))
    .filter((item) => item.end > item.start);

  return basePeriods.flatMap((period) => {
    const overlapping = blockRanges.filter((item) => item.start < period.end && item.end > period.start);
    if (!overlapping.length) return [period];
    if (period.appointments.length) {
      const primary = overlapping.find((item) => item.block.scope === "all_services") || overlapping[0];
      return [{ ...period, block: primary.block }];
    }

    const boundaries = new Set<number>([period.start, period.end]);
    overlapping.forEach((item) => {
      boundaries.add(Math.max(period.start, item.start));
      boundaries.add(Math.min(period.end, item.end));
    });
    const points = [...boundaries].sort((left, right) => left - right);
    return points
      .slice(0, -1)
      .map((start, pointIndex) => {
        const end = points[pointIndex + 1];
        const matching = overlapping.filter((item) => item.start < end && item.end > start);
        const block = (matching.find((item) => item.block.scope === "all_services") || matching[0])?.block || null;
        return { start, end, appointments: [], block };
      })
      .filter((item) => item.end > item.start);
  });
}

export function isBookableFreePeriod(period: SchedulePeriod, minimumStartMinutes?: number) {
  return (
    period.appointments.length === 0 &&
    !period.block &&
    (minimumStartMinutes === undefined || period.end > minimumStartMinutes)
  );
}
