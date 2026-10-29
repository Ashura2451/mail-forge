# React operator console context

- `App.tsx` is currently the feature shell and workbench composition point.
- Keep API calls through `api`, `upload`, `uploadMany`, or
  `uploadWithFields` from `src/api/client.ts`.
- Prefer stable view IDs and existing service routes. Browser navigation must
  survive refresh and must not move business state into the client.
- Use semantic landmarks, labelled controls, visible focus, text status
  labels, and `aria-live` for asynchronous feedback.
- Follow `DESIGN.md`: calm local operations-console language, teal primary
  actions, amber attention, coral unresolved/destructive state, and usable
  dense tables at narrow widths.
