# Mailforge local web application design contract

## Product character

Mailforge is a focused local email utility for people who need to sort address
files, compare lists, or prepare a controlled send. The interface should feel
calm, explicit, and inspectable: every state has a source, every risky action
has a clear mode, and empty or unavailable data explains what the operator can
do next.

The primary user may already have an HTML email and a recipient file. They
should not need Python, SQLite, SMTP, campaign, CRM, or marketing-platform
knowledge to use the first web slice. The product is a local browser
application, not a hosted multi-tenant service.

## Visual direction

Use an editorial utility direction: warm off-white canvas, ink navy text,
slate borders, and teal as the primary action color. Amber marks
attention and coral marks destructive or unresolved outcomes. The visual
language is inspired by accessible documentation and operations products such
as GitHub Primer, but Mailforge uses its own palette and terminology.

The design research compared two directions. A dense persistent operations rail
would preserve the previous service-oriented console, but made 17 destinations
compete before a user chose a job. A task-first workspace, informed by the
GOV.UK task-list guidance (short task names, visible sentence-case status, and
user-controlled order), keeps Sort email, Compare lists, and Send visible while
placing account and recovery utilities behind More tools. This direction was
selected because the primary operations are independent and do not require a
fixed wizard. WCAG 2.2 label guidance and Primer's skip/navigation patterns
reinforce persistent labels, visible focus, and explicit state text.

Avoid glossy gradients, oversized hero art, decorative charts, and competitor
branding. Density should support scanning tables and status cards without
feeling like a spreadsheet.

## Layout and responsive behavior

- Desktop: persistent 248px navigation rail, 32px page gutter, content capped
  at 1440px, and two-column dashboard panels where data supports it.
- Tablet: navigation compresses to a top bar; cards become one or two columns.
- Small screens: one-column flow, horizontally scrollable dense tables, and
  full-width primary actions. Never hide a required action only behind hover.
- Use an 8px spacing scale with 16px default card padding and 24px section
  rhythm.

## Typography and tokens

Use the system sans stack for reliable local operation. Headings are compact
and confident; body copy is plain and direct. The implementation owns semantic
tokens in `web/src/App.css` rather than scattering raw colors through
components.

Core roles:

- canvas: `#f5f7f6`; surface: `#ffffff`; ink: `#17211f`; muted: `#64736f`;
- line: `#dce5e2`; primary: `#087f73`; primary-strong: `#056056`;
- attention: `#b7791f`; danger: `#bd4b4b`; success: `#197a55`;
- focus: `#1b9aaa` with a visible 3px outline.

Use 12px labels, 14px supporting text, 15–16px body text, 20–28px section
headings, and 32px page titles. Use `tabular-nums` for counts and timestamps.

## Components and interaction states

- Navigation: icon-free text labels with an active rail and a visible current
  location. The primary path is Home, Sort email, Compare lists, Send, and
  History. Email accounts, Saved lists, and Settings remain available under an
  explicit More tools group.
- More tools is collapsed on Home and expands on demand or when a utility route
  is opened directly. It is progressive disclosure for optional conveniences,
  not a second product area.
- Status badges: always include text, never color alone. Use `Draft`, `Ready`,
  `Running`, `Paused`, `Completed`, `Needs review`, and `Unverified` exactly.
- Cards: one clear title, one primary value, one supporting sentence, and an
  optional action. Do not invent metrics when the API has no value.
- Forms: labels above controls, help text below, inline field errors, and a
  summary error at the top for failed submissions.
- Tables: meaningful column headings, row focus, stable empty states, and
  explicit loading/error/retry states.
- Confirmations: use a dialog for cancellation and other consequential state
  changes; simulation authorization must name the mode.

## Content and safety language

Prefer “simulation”, “local protocol test”, “provider acceptance”, and
“inbox delivery is not verified”. Never label a fake SMTP acceptance as sent.
Unknown authorization evidence is shown as `authorization_unknown`, with a
short explanation rather than a consent implication.

Errors should say what failed, at which boundary, and what the operator can do
next. Avoid “something went wrong” and avoid exposing exception traces,
credentials, raw message bodies, or database paths in ordinary UI.

## Accessibility

Use semantic landmarks, one `h1` per route, keyboard-accessible controls,
visible focus, sufficient contrast, labelled dialogs, `aria-live` status
updates for background progress, and reduced-motion support. Color is never
the sole status signal. Browser acceptance must exercise keyboard navigation
and a narrow viewport.

## Implementation and verification

React components live under `web/src/`; API calls are centralized in
`web/src/api/client.ts`; backend business rules remain in `src/mailforge`.
The home route is task-led and must not require SMTP setup for standalone list
cleaning. Account setup uses plain labels for sender email, SMTP server,
username, password, port, and encryption; credential references are advanced
storage details rather than first-use concepts.
Production assets are built by Vite into the backend-owned static directory
and served by FastAPI. Read this contract before adding screens, and verify
the rendered app at desktop and narrow widths before declaring the slice done.
