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

The approved direction is a direct-manipulation productivity utility. A prior
task-led workbench was rejected because it made users manage work items before
touching their files. The retained experience treats address files, provider
groups, lists, message content, and recipients as the primary objects. Addresses,
Compare, and Compose are independent top-level tools; Activity, Accounts, Saved
lists, and Settings are supporting utilities revealed contextually. Provenance,
preparation, persistence, authorization, and recovery remain automatic service
mechanisms and appear only when they help a decision.

The object-first layout draws on file managers, spreadsheet tables, document
editors, and email composition utilities: dense, readable data in the main
workspace; selection controls beside the data they affect; and contextual
inspection panels rather than permanent multi-pane administration. Provider
groups are a first-class Sorter object, not a summary metric. WCAG 2.2 label
guidance and visible focus remain required, but the information hierarchy is
set by the operator's data and immediate next action.

Avoid glossy gradients, oversized hero art, decorative charts, and competitor
branding. Density should support scanning tables and status cards without
feeling like a spreadsheet.

## Layout and responsive behavior

- Desktop: compact 218px navigation rail, 20–52px page gutter, content capped
  at 1440px. Sorter gives provider groups a contextual side panel and the
  address table the largest workspace. Compare and Compose use layouts suited
  to their sources and message content rather than inheriting Sorter's panes.
- Tablet: navigation becomes a dismissible top-bar drawer; tables retain their
  headers and scroll as a data surface; supporting panels stack below the main
  operation.
- Small screens: one-column task layouts, horizontally scrollable dense tables,
  collapsible supporting content, and full-width primary actions. No required
  action is only available through hover.
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
  location. The primary path is Addresses, Compare, and Compose. Activity,
  Accounts, Saved lists, and Settings remain available under an explicit More
  tools group. Home is a compact files-and-lists starting surface, not an
  administrative dashboard.
- More tools is collapsed on Home and expands on demand or when a utility route
  is opened directly. It is progressive disclosure for optional conveniences,
  not a second product area.
- Status badges: always include text, never color alone. Use `Draft`, `Ready`,
  `Running`, `Paused`, `Completed`, `Needs review`, and `Unverified` exactly.
- Cards: use only for an object or decision that benefits from grouping. One
  clear title, one primary value, one supporting sentence, and an optional
  action. Do not invent metrics when the API has no value. Dense tables are the
  default for address records, lists, activity and prepared recipients.
- Forms: labels above controls, help text below, inline field errors, and a
  summary error at the top for failed submissions.
- Tables: meaningful column headings, row focus, exact selection semantics,
  stable empty states, bounded loading/error/retry states, and an explicit
  strategy for large files. Filtering visible rows never silently changes a
  saved or transferred selection.
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
The home route is a compact files-and-lists start surface and must not require
SMTP setup for standalone list cleaning. Account setup uses plain labels for sender email, SMTP server,
username, password, port, and encryption; credential references are advanced
storage details rather than first-use concepts.
Production assets are built by Vite into the backend-owned static directory
and served by FastAPI. Read this contract before adding screens, and verify
the rendered app at desktop and narrow widths before declaring the slice done.
