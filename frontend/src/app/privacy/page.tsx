import type { Metadata } from "next";
import Link from "next/link";

import { resolvePublicLegalName } from "@/lib/public-brand";

export const metadata: Metadata = {
  title: "Privacy Policy | Linka",
  description:
    "Privacy Policy for Linka clinic operations, customer service, booking, and messaging services.",
};

const effectiveDate = "August 16, 2026";

function Section({
  title,
  children,
}: Readonly<{
  title: string;
  children: React.ReactNode;
}>) {
  return (
    <section className="space-y-3 border-t border-[var(--border)] pt-7 first:border-t-0 first:pt-0">
      <h2 className="text-xl font-semibold tracking-tight text-[var(--text)]">
        {title}
      </h2>
      <div className="space-y-3 text-sm leading-7 text-[var(--muted)] sm:text-[15px]">
        {children}
      </div>
    </section>
  );
}

function List({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <ul className="list-disc space-y-2 pr-5 marker:text-[var(--accent)]">
      {children}
    </ul>
  );
}

export default function PrivacyPolicyPage() {
  const legalName = resolvePublicLegalName(process.env.NEXT_PUBLIC_TIA_LEGAL_NAME);
  const privacyEmail = process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL?.trim();

  return (
    <main className="min-h-screen bg-[var(--bg)] px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-4xl">
        <header className="mb-6 rounded-3xl border border-[var(--border)] bg-[var(--surface)] p-6 shadow-sm sm:p-9">
          <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
            <div className="space-y-3">
              <p className="text-sm font-semibold text-[var(--accent)]">Linka</p>
              <h1 className="text-3xl font-bold tracking-tight text-[var(--text)] sm:text-4xl">
                Privacy Policy
              </h1>
              <p className="max-w-2xl text-sm leading-7 text-[var(--muted)] sm:text-base">
                This policy explains how {legalName} handles information when its
                clinic operations, customer-service, booking, messaging, and AI
                assistance features are used.
              </p>
            </div>
            <div className="rounded-2xl bg-[var(--surface-2)] px-4 py-3 text-sm text-[var(--muted)]">
              <div className="font-medium text-[var(--text)]">Effective date</div>
              <div>{effectiveDate}</div>
            </div>
          </div>

          <div className="mt-7 rounded-2xl border border-[var(--border)] bg-[var(--accent-soft)] p-4 text-sm leading-7 text-[var(--text)]">
            <strong>ملخص بالعربي:</strong> Linka بتستخدم بيانات العملاء والمحادثات
            والحجوزات علشان تشغّل خدمة العملاء والحجز وتساعد فريق العيادة. البيانات
            لا يتم بيعها للمعلنين. أي أسئلة طبية تشخيصية أو عن ملاءمة علاج معيّن
            يتم تصعيدها لفريق بشري بدل اتخاذ قرار طبي آلي.
          </div>
        </header>

        <article className="space-y-8 rounded-3xl border border-[var(--border)] bg-[var(--surface)] p-6 shadow-sm sm:p-9">
          <Section title="1. Who this policy applies to">
            <p>
              {legalName} provides software that clinics and other authorized
              businesses can use to communicate with customers, manage customer
              records and appointments, automate operational workflows, and use AI
              assistance. The clinic or business operating a Linka workspace is
              generally responsible for the customer information it places in that
              workspace, while {legalName} processes that information to provide and
              secure the service.
            </p>
          </Section>

          <Section title="2. Information we process">
            <p>Depending on how a workspace is configured, we may process:</p>
            <List>
              <li>
                Business account information such as workspace members, roles,
                clinic branches, services, doctors, working hours, and booking
                settings.
              </li>
              <li>
                Customer and CRM information such as name, phone number,
                conversation history, notes, and identifiers supplied by
                connected messaging services.
              </li>
              <li>
                Appointment information such as requested service, branch, doctor,
                date, time, booking status, and related operational notes.
              </li>
              <li>
                Communications sent through connected channels, including WhatsApp
                message content and message delivery/read status where available.
              </li>
              <li>
                Technical and security information such as timestamps, integration
                identifiers, delivery attempts, audit events, error information,
                and authentication or authorization metadata.
              </li>
            </List>
            <p>
              A customer may choose to include health-related information in a
              message. Linka is designed for clinic operations and customer
              service, not automated medical diagnosis. Requests involving medical
              diagnosis, treatment suitability, or urgent symptoms are intended to
              be escalated to qualified human staff rather than decided by the AI.
            </p>
          </Section>

          <Section title="3. How we use information">
            <p>We process information as needed to:</p>
            <List>
              <li>Provide customer support and conversational assistance.</li>
              <li>Create, update, confirm, reschedule, or cancel appointments.</li>
              <li>Maintain CRM and operational records for the relevant workspace.</li>
              <li>
                Route conversations to human team members when escalation or manual
                review is appropriate.
              </li>
              <li>
                Run configured reminders, notifications, delivery workflows, and
                other business automations.
              </li>
              <li>
                Protect the service, enforce access controls, investigate failures,
                prevent misuse, and maintain auditability.
              </li>
              <li>
                Operate AI-assisted features that interpret customer requests and
                produce responses or structured workflow decisions.
              </li>
            </List>
          </Section>

          <Section title="4. AI-assisted processing">
            <p>
              When AI features are enabled, relevant message content and necessary
              workspace context may be sent to configured AI model providers so the
              service can understand a request, generate a response, or produce a
              structured action plan. Linka applies application-level validation,
              access controls, and business rules around AI outputs before business
              actions are executed.
            </p>
            <p>
              AI-generated output is not intended to replace professional medical
              judgment. Medical diagnosis, suitability decisions, and urgent
              clinical concerns should be handled by qualified human professionals.
            </p>
          </Section>

          <Section title="5. WhatsApp and other connected services">
            <p>
              If a workspace connects WhatsApp, messages and related metadata may
              be exchanged with Meta&apos;s WhatsApp services to receive customer
              messages and send replies. Meta processes information under its own
              terms and privacy policies.
            </p>
            <p>
              A workspace may also connect automation, email, calendar, hosting,
              database, monitoring, or other service providers. We share only the
              information reasonably necessary for those providers to perform the
              configured service.
            </p>
          </Section>

          <Section title="6. Sharing and disclosure">
            <p>We may disclose information to:</p>
            <List>
              <li>
                Authorized members of the clinic or business workspace that serves
                the customer.
              </li>
              <li>
                Service providers that host, transmit, automate, secure, monitor,
                or provide AI functionality for Linka.
              </li>
              <li>
                Messaging and communications providers such as Meta/WhatsApp when
                the relevant integration is enabled.
              </li>
              <li>
                Authorities or other parties when disclosure is required by law or
                reasonably necessary to protect rights, safety, security, or the
                integrity of the service.
              </li>
            </List>
            <p>
              We do not sell customer conversation or appointment data to
              advertisers.
            </p>
          </Section>

          <Section title="7. Data retention">
            <p>
              Information is retained for as long as reasonably necessary to
              provide the workspace service, maintain business and security records,
              comply with applicable obligations, resolve disputes, and enforce
              agreements. Retention may vary based on the type of record and the
              requirements of the clinic or business operating the workspace.
            </p>
          </Section>

          <Section title="8. Security and access control">
            <p>
              Linka uses technical and organizational controls intended to protect
              information, including workspace-level access control, role-based
              permissions, authenticated integration access, audit trails, and
              protected transport of data between services. No method of storage or
              transmission can be guaranteed to be completely secure.
            </p>
          </Section>

          <Section title="9. International processing">
            <p>
              Connected infrastructure or service providers may process information
              in countries other than the customer&apos;s country. Where applicable,
              the responsible business should use appropriate safeguards for such
              transfers and comply with local data-protection requirements.
            </p>
          </Section>

          <Section title="10. Your choices and privacy rights">
            <p>
              Depending on applicable law, individuals may have rights to request
              access, correction, deletion, restriction, objection, or other action
              relating to their personal information. Customers should normally
              contact the clinic or business they communicated with because that
              organization controls the customer relationship and its workspace
              records.
            </p>
          </Section>

          <Section title="11. Children">
            <p>
              Linka is a business software service and is not directed to children
              for independent use. Clinics and businesses using the service are
              responsible for applying any consent, guardian, or age requirements
              that apply to their services and customers.
            </p>
          </Section>

          <Section title="12. Changes to this policy">
            <p>
              We may update this policy as the service, integrations, or legal
              requirements change. The effective date at the top of this page will
              be updated when material changes are published.
            </p>
          </Section>

          <Section title="13. Contact">
            {privacyEmail ? (
              <p>
                For privacy questions about the Linka platform, contact us at{" "}
                <a
                  className="font-medium text-[var(--accent)] underline underline-offset-4"
                  href={`mailto:${privacyEmail}`}
                >
                  {privacyEmail}
                </a>
                . Customers may also contact the clinic or business they interacted
                with for requests relating to that organization&apos;s workspace data.
              </p>
            ) : (
              <p>
                For requests about customer records, contact the clinic or business
                you interacted with using its published business contact details.
                Workspace administrators can contact Linka through their normal
                account-support channel for platform privacy requests.
              </p>
            )}
          </Section>
        </article>

        <footer className="flex flex-col gap-3 px-2 py-8 text-xs leading-6 text-[var(--muted)] sm:flex-row sm:items-center sm:justify-between">
          <span>© 2026 {legalName}. Privacy policy.</span>
          <Link
            className="font-medium text-[var(--accent)] hover:underline"
            href="/login"
          >
            Linka login
          </Link>
        </footer>
      </div>
    </main>
  );
}
