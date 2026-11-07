# Fleet agent entry point

READ FIRST: `FLEET_CREED.md` in this repo root. It is the constitution — roles,
design language (Design B), API contract, build flow, and guardrails. It
outranks your preferences. Follow it for all work in this repo.

The Roles table lists default owners, not locks. This is a hybrid repo
worked on by many agents: work in whatever area your task needs, and keep
changes to what the task requires. Report unrelated problems to the
orchestrator instead of fixing them on the side.

Key pointers:
- UI work → see `ios/DESIGN_B_IMPLEMENT.md` (current brief) and
  `ios/SWIFTUI_DESIGN.md` (app architecture)
- API contract → `web/lib/types.ts` + `web/lib/apiClient.ts` (the law)
- Design mockups → `ios/ui-concepts/design-b-dashboard-light.html`
- Never commit secrets. `ios/Config.xcconfig` is gitignored.
