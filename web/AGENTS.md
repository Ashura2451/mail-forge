# React operator console context

- `App.tsx` is the focused feature shell and workbench composition point.
- Keep API calls through `api`, `upload`, `uploadMany`, or
  `uploadWithFields` from `src/api/client.ts`.
- Prefer stable view IDs and existing service routes. Browser navigation must
  survive refresh and must not move business state into the client.
- The primary views are Home, Sort email, Compare lists, Send, and History.
  Email accounts, Saved lists, and Settings are optional utility views. Do not
  reintroduce retired marketing, CRM, mailbox, automation, or general data
  studio destinations into ordinary navigation without a new approved scope.
- Use semantic landmarks, labelled controls, visible focus, text status
  labels, and `aria-live` for asynchronous feedback.
- Follow `DESIGN.md`: calm local operations-console language, teal primary
  actions, amber attention, coral unresolved/destructive state, and usable
  dense tables at narrow widths.
