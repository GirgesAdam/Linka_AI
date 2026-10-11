import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const baseURL = process.env.AUDIT_FRONTEND_URL || 'http://127.0.0.1:3000';
const outputDir = path.resolve('audit-output');
const shots = path.join(outputDir, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const agentPath = path.join(outputDir, 'agent-db-evidence.json');
const agentEvidence = fs.existsSync(agentPath) ? JSON.parse(fs.readFileSync(agentPath, 'utf8')) : { scenarios: {} };
const probeEvidence = JSON.parse(fs.readFileSync(path.join(outputDir, 'ui-probe-evidence.json'), 'utf8'));
function scenarioEvidence(key) {
  const actual = agentEvidence.scenarios?.[key];
  if (actual?.appointments?.length) return { ...actual, evidence_source: 'agent' };
  return probeEvidence.scenarios[key];
}
const scenarioA = scenarioEvidence('A');
const scenarioB = scenarioEvidence('B');
const scenarioC = scenarioEvidence('C');

function countText(haystack, needle) {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

async function snapshot(page, name, url) {
  await page.goto(`${baseURL}${url}`, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(700);
  const text = await page.locator('body').innerText();
  await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
  return { url, text };
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'ar-EG' });
  const page = await context.newPage();
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' });
  await page.locator('input[name="email"]').fill('audit.reception@linka.local');
  await page.locator('input[name="password"]').fill('AuditPass123!');
  await page.locator('form button[type="submit"]').click();
  await page.waitForTimeout(5000);
  if (new URL(page.url()).pathname.startsWith('/login')) {
    const loginText = await page.locator('body').innerText();
    await page.screenshot({ path: path.join(shots, 'login-debug.png'), fullPage: true });
    fs.writeFileSync(path.join(outputDir, 'login-debug.json'), JSON.stringify({ url: page.url(), body_text: loginText }, null, 2));
    throw new Error(`Audit login did not leave /login: ${page.url()}`);
  }

  const ui = { login_final_url: page.url(), scenarios: {}, warning: 'UI screenshots may use supplemental grouped rows when Agent booking failed; see evidence_source.' };

  const aFirst = scenarioA.appointments[0];
  const aDate = aFirst.start_local.slice(0, 10);
  const dashboard = await snapshot(page, 'scenario-a-dashboard-desktop', '/dashboard');
  const appointments = await snapshot(page, 'scenario-a-appointments-desktop', `/appointments?date=${encodeURIComponent(aDate)}&branch_id=${encodeURIComponent(aFirst.branch_id)}`);
  const detail = await snapshot(page, 'scenario-a-detail-desktop', `/appointments/${aFirst.appointment_id}`);
  const patient = await snapshot(page, 'scenario-a-patient-desktop', `/patients/${scenarioA.patient.id}`);

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'ar-EG', storageState: await context.storageState() });
  const mobilePage = await mobile.newPage();
  const appointmentsMobile = await snapshot(mobilePage, 'scenario-a-appointments-mobile', `/appointments?date=${encodeURIComponent(aDate)}&branch_id=${encodeURIComponent(aFirst.branch_id)}`);

  const aServices = scenarioA.appointments.map((row) => row.service_name);
  ui.scenarios.A = {
    evidence_source: scenarioA.evidence_source,
    dashboard: {
      patient_name_occurrences: countText(dashboard.text, scenarioA.patient.name),
      service_occurrences: Object.fromEntries(aServices.map((name) => [name, countText(dashboard.text, name)])),
      body_text: dashboard.text,
    },
    appointments: {
      patient_name_occurrences: countText(appointments.text, scenarioA.patient.name),
      service_occurrences: Object.fromEntries(aServices.map((name) => [name, countText(appointments.text, name)])),
      body_text: appointments.text,
    },
    appointments_mobile: {
      patient_name_occurrences: countText(appointmentsMobile.text, scenarioA.patient.name),
      service_occurrences: Object.fromEntries(aServices.map((name) => [name, countText(appointmentsMobile.text, name)])),
      body_text: appointmentsMobile.text,
    },
    detail: {
      patient_name_occurrences: countText(detail.text, scenarioA.patient.name),
      service_occurrences: Object.fromEntries(aServices.map((name) => [name, countText(detail.text, name)])),
      body_text: detail.text,
    },
    patient: {
      patient_name_occurrences: countText(patient.text, scenarioA.patient.name),
      service_occurrences: Object.fromEntries(aServices.map((name) => [name, countText(patient.text, name)])),
      body_text: patient.text,
    },
  };

  // Controls B and C: collect UI text from their appointment date views and patient profiles.
  for (const [key, scenario] of [['B', scenarioB], ['C', scenarioC]]) {
    const first = scenario.appointments[0];
    const date = first.start_local.slice(0, 10);
    const ap = await snapshot(page, `scenario-${key.toLowerCase()}-appointments-desktop`, `/appointments?date=${encodeURIComponent(date)}&branch_id=${encodeURIComponent(first.branch_id)}`);
    const pp = await snapshot(page, `scenario-${key.toLowerCase()}-patient-desktop`, `/patients/${scenario.patient.id}`);
    const services = scenario.appointments.map((row) => row.service_name);
    ui.scenarios[key] = {
      evidence_source: scenario.evidence_source,
      appointments: {
        patient_name_occurrences: countText(ap.text, scenario.patient.name),
        service_occurrences: Object.fromEntries(services.map((name) => [name, countText(ap.text, name)])),
        full_arms_occurrences: countText(ap.text, 'ليزر إزالة الشعر - ذراعين كاملين'),
        body_text: ap.text,
      },
      patient: {
        patient_name_occurrences: countText(pp.text, scenario.patient.name),
        service_occurrences: Object.fromEntries(services.map((name) => [name, countText(pp.text, name)])),
        full_arms_occurrences: countText(pp.text, 'ليزر إزالة الشعر - ذراعين كاملين'),
        body_text: pp.text,
      },
    };
  }

  fs.writeFileSync(path.join(outputDir, 'ui-evidence.json'), JSON.stringify(ui, null, 2), 'utf8');
  await mobile.close();
  await browser.close();
  console.log('UI_EVIDENCE=' + JSON.stringify(ui));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
