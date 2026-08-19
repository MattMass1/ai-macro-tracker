# Fleet agent entry point

READ FIRST: `FLEET_CREED.md` in this repo root. It is the constitution — roles,
design language (Design B), API contract, build flow, and guardrails. It
outranks your preferences. Follow it for all work in this repo.

Your role is defined in the Roles table. Stay in your lane. Report
cross-role problems to the orchestrator; don't fix them yourself.

Key pointers:
- UI work → see `ios/DESIGN_B_IMPLEMENT.md` (current brief) and
  `ios/SWIFTUI_DESIGN.md` (app architecture)
- API contract → `web/lib/types.ts` + `web/lib/apiClient.ts` (the law)
- Design mockups → `ios/ui-concepts/design-b-dashboard-light.html`
- Never commit secrets. `ios/Config.xcconfig` is gitignored.
