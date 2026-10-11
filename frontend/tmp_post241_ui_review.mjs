import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const baseURL = process.env.AUDIT_FRONTEND_URL || 'http://127.0.0.1:3000';
const auditEmail = process.env.AUDIT_UI_EMAIL;
const auditPassword = process.env.AUDIT_UI_PASSWORD;
if (!auditEmail || !auditPassword) throw new Error('Missing audit UI credentials');
const outputDir = path.resolve('audit-output');
const shots = path.join(outputDir, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const evidence = JSON.parse(fs.readFileSync(path.join(outputDir, 'ui-agent-evidence.json'), 'utf8'));
if (!evidence.all_agent_created) throw new Error('UI evidence contains a non-Agent appointment');
for (const key of ['A', 'B', 'C']) if (!evidence.bookings?.[key]?.appointment) throw new Error(`Missing Agent-created booking ${key}`);
const A = evidence.bookings.A.appointment;
const B = evidence.bookings.B.appointment;
const C = evidence.bookings.C.appointment;
const patient = evidence.patient;

function countText(haystack, needle) {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

async function snapshot(page, name, url) {
  await page.goto(`${baseURL}${url}`, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(700);
  const text = await page.locator('body').innerText();
  const appointmentLinks = await page.locator('a[href*="/appointments/"]').evaluateAll((nodes) => nodes.map((n) => n.getAttribute('href')).filter(Boolean));
  await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
  return { url, text, appointment_links: appointmentLinks };
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'ar-EG', ignoreHTTPSErrors: true });
  const tokenResponse = await context.request.post('https://example.supabase.co/auth/v1/token?grant_type=password', {
    data: { email: auditEmail, password: auditPassword },
    headers: { apikey: 'sb_publishable_ci_dummy' },
  });
  if (!tokenResponse.ok()) throw new Error(`Audit token request failed: ${tokenResponse.status()}`);
  const session = await tokenResponse.json();
  const encoded = 'base64-' + Buffer.from(JSON.stringify(session), 'utf8').toString('base64url');
  const baseCookie = 'sb-example-auth-token';
  const cookieChunk = 3000;
  const authCookies = [];
  if (encoded.length <= cookieChunk) authCookies.push({ name: baseCookie, value: encoded, url: baseURL, sameSite: 'Lax' });
  else for (let i = 0, n = 0; i < encoded.length; i += cookieChunk, n += 1) authCookies.push({ name: `${baseCookie}.${n}`, value: encoded.slice(i, i + cookieChunk), url: baseURL, sameSite: 'Lax' });
  authCookies.push({ name: 'tia_workspace_id', value: evidence.workspace_id, url: baseURL, sameSite: 'Lax', httpOnly: true });
  await context.addCookies(authCookies);
  const page = await context.newPage();
  await page.goto(`${baseURL}/dashboard`, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(700);
  if (new URL(page.url()).pathname.startsWith('/login')) {
    await page.screenshot({ path: path.join(shots, 'login-debug.png'), fullPage: true });
    throw new Error(`Audit auth remained on login: ${page.url()}`);
  }

  const tuesday = A.start_local.slice(0, 10);
  const sunday = C.start_local.slice(0, 10);
  const dashboard = await snapshot(page, 'dashboard-desktop', '/dashboard');
  const appointments = await snapshot(page, 'appointments-tuesday-desktop', `/appointments?date=${encodeURIComponent(tuesday)}&branch_id=${encodeURIComponent(A.branch_id)}`);
  const detail = await snapshot(page, 'appointment-a-detail-desktop', `/appointments/${A.appointment_id}`);
  const profile = await snapshot(page, 'patient-profile-desktop', `/patients/${patient.id}`);
  const correctionPage = await snapshot(page, 'appointments-correction-sunday-desktop', `/appointments?date=${encodeURIComponent(sunday)}&branch_id=${encodeURIComponent(C.branch_id)}`);
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'ar-EG', storageState: await context.storageState(), ignoreHTTPSErrors: true });
  const mobilePage = await mobile.newPage();
  const appointmentsMobile = await snapshot(mobilePage, 'appointments-tuesday-mobile', `/appointments?date=${encodeURIComponent(tuesday)}&branch_id=${encodeURIComponent(A.branch_id)}`);

  const aAdditional = A.additional_services.map((row) => row.service_name);
  const allA = [A.service_name, ...aAdditional];
  const expectedTuesdayIds = [A.appointment_id, B.appointment_id];
  const expectedProfileIds = [A.appointment_id, B.appointment_id, C.appointment_id];
  const linkContains = (links, id) => links.some((href) => href.includes(id));
  const ui = {
    login_final_url: page.url(),
    evidence_source: 'real-agent-created-bookings-only',
    patient,
    booking_ids: { A: A.appointment_id, B: B.appointment_id, C: C.appointment_id },
    appointments_page: {
      date: tuesday,
      patient_name_occurrences: countText(appointments.text, patient.name),
      main_service_occurrences: countText(appointments.text, A.service_name),
      additional_service_occurrences: Object.fromEntries(aAdditional.map((name) => [name, countText(appointments.text, name)])),
      booking_links_present: Object.fromEntries(expectedTuesdayIds.map((id) => [id, linkContains(appointments.appointment_links, id)])),
      body_text: appointments.text,
    },
    appointments_mobile: {
      patient_name_occurrences: countText(appointmentsMobile.text, patient.name),
      main_service_occurrences: countText(appointmentsMobile.text, A.service_name),
      additional_service_occurrences: Object.fromEntries(aAdditional.map((name) => [name, countText(appointmentsMobile.text, name)])),
      booking_links_present: Object.fromEntries(expectedTuesdayIds.map((id) => [id, linkContains(appointmentsMobile.appointment_links, id)])),
      body_text: appointmentsMobile.text,
    },
    detail: {
      patient_name_occurrences: countText(detail.text, patient.name),
      service_occurrences: Object.fromEntries(allA.map((name) => [name, countText(detail.text, name)])),
      body_text: detail.text,
    },
    patient_profile: {
      patient_name_occurrences: countText(profile.text, patient.name),
      booking_links_present: Object.fromEntries(expectedProfileIds.map((id) => [id, linkContains(profile.appointment_links, id)])),
      body_text: profile.text,
    },
    correction_ui: {
      date: sunday,
      patient_name_occurrences: countText(correctionPage.text, patient.name),
      combo_occurrences: countText(correctionPage.text, C.service_name),
      removed_full_arms_occurrences: countText(correctionPage.text, 'ليزر إزالة الشعر - ذراعين كاملين'),
      body_text: correctionPage.text,
    },
    dashboard: {
      patient_name_occurrences: countText(dashboard.text, patient.name),
      body_text: dashboard.text,
      note: 'No forced same-day booking; dashboard may be N/A because the audit clock is late Sunday.',
    },
  };
  ui.assertions = {
    same_patient_two_separate_tuesday_bookings_visible: ui.appointments_page.patient_name_occurrences >= 2 && expectedTuesdayIds.every((id) => ui.appointments_page.booking_links_present[id]),
    multi_service_detail_shows_all_services: allA.every((name) => ui.detail.service_occurrences[name] > 0),
    correction_removed_service_not_visible_on_c_booking_day: ui.correction_ui.removed_full_arms_occurrences === 0,
    profile_contains_three_real_booking_links: expectedProfileIds.every((id) => ui.patient_profile.booking_links_present[id]),
  };
  ui.pass = Object.values(ui.assertions).every(Boolean);
  fs.writeFileSync(path.join(outputDir, 'ui-evidence.json'), JSON.stringify(ui, null, 2), 'utf8');
  await mobile.close();
  await browser.close();
  console.log('UI_EVIDENCE=' + JSON.stringify({ pass: ui.pass, assertions: ui.assertions, patient: ui.patient, booking_ids: ui.booking_ids }));
  if (!ui.pass) process.exit(2);
}

main().catch((error) => { console.error(error); process.exit(1); });
