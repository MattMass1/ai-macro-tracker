# Terra backend setup: credential boundary

This candidate is local implementation, not activation or deployment approval.
Keep `FOOD_AGENT_ENABLED=false` until independent review and explicitly approved,
capped live evaluation are complete. No API key belongs in iOS, a source file,
a benchmark result, or chat.

## App route

Only the new food interpreter changes:

| Setting | Before | Candidate |
|---|---|---|
| Model selection | `FOOD_AGENT_MODEL`, then `COACH_MODEL`, then `gpt-5.6-luna` | `FOOD_AGENT_MODEL`, otherwise exactly `gpt-6.1-terra` |
| Endpoint | Chat Completions via coach transport | `https://api.openai.com/v1/responses` via HTTPX |
| Credential | `OPENAI_ACCESS_TOKEN` | nonblank `OPENAI_API_KEY` first; nonblank `OPENAI_ACCESS_TOKEN` compatibility second |
| Output | strict JSON schema | Responses `text.format`, strict JSON schema, Pydantic validation |

An explicitly injected test token takes precedence over environment values. The
production factory injects no token. Whitespace-only values are absent. Missing
both environment credentials raises `InterpreterError` before HTTP. No login,
account discovery, alternate provider or SDK is used.

Starting settings: `store:false`, `service_tier:default`,
`reasoning:{effort:none}`, `max_output_tokens:2000`, 30-second HTTP timeout, no
automatic retries, redirects or proxy-environment inheritance. These are bounded
starting settings, not measured optimal settings. Only a completed response with
one completed assistant message containing one output-text JSON object is
accepted. Refusals, incomplete/failed responses, tool calls, multiple messages,
malformed JSON and invalid actions cause no model-authorized write.

The existing CanvasService factory already constructs `ModelInterpreter`; the
mocked protocol is exercised through that real turn path. Gate-off behavior and
legacy `COACH_MODEL`/web extraction/fallback defaults are unchanged. No migration,
iOS change or public API contract change is required.

## Eventual owner steps (not performed here)

1. Have the independent reviewer verify the exact local candidate commit. Keep
   the feature disabled. The writer does not approve its own work.
2. When deployment/configuration work is separately authorized, enter the app's
   OpenAI key in the **Render backend web service > Environment > OPENAI_API_KEY**.
   Use Render's secret input, not chat. Existing `OPENAI_ACCESS_TOKEN` remains a
   compatibility route; if both are present the new interpreter uses
   `OPENAI_API_KEY`. This does not change the old coach or web lookup credential.
3. Keep these nonsecret app settings explicit:
   - `FOOD_AGENT_MODEL=gpt-6.1-terra`
   - `FOOD_AGENT_ENABLED=false`
   - Do not change `COACH_MODEL`, `WEB_LOOKUP_EXTRACT_MODEL`, provider permissions,
     or the existing account merely to satisfy a test. Review any needed route
     configuration separately. FatSecret remains the selected food provider.
4. Only after review and a separate live-evaluation go, supply approved backend
   credentials to the disposable runner using an approved secret-loading path.
   Do not copy them from another profile/account or load a developer `.env`.
5. Review the actual live scores, latency and cost bounds before any canary
   enablement. Then separately authorize deployment and an owner-scoped canary.
   Adding a key alone neither deploys nor enables this feature.

No local credential-presence result proves what is configured in production.
Account access to the exact model, live quality, real provider behavior and phone
rendering remain unverified.

## Live harness launch requirements

`server/scripts/food_agent_bench.py --lane C` performs preflight before loading
bench data, creating a disposable database, or calling providers. The callable
runner also refuses before case setup. Requirements include:

- Explicit `FOOD_AGENT_MODEL=gpt-6.1-terra`; new interpreter credential above.
- FatSecret attribution permission enabled and the existing client-ID/secret or
  consumer-key/secret pair. `FATSECRET_CACHE_ALLOWED` is not changed by the runner.
- Explicit existing `WEB_LOOKUP_PROVIDER` and `COACH_MODEL` for fallback. The
  existing web route requires `OPENAI_ACCESS_TOKEN`; when Exa is selected it also
  requires `EXA_API_KEY` and an explicitly priced
  `WEB_LOOKUP_EXTRACT_MODEL=gpt-6.1-terra`. This is a prerequisite, not permission
  to silently change an unrelated route. An unpriced extraction route refuses.
- A local disposable `--pg-admin-url`; remote endpoints, service-file overrides,
  remote host-address overrides and output paths inside the worktree refuse.
- `--rate-card /approved/nonsecret-rates.json` with independently reviewed prices
  covering **all** enabled paths, including hosted-search fallback. There is no
  bundled live account rate card. Unknown costs are never assumed free.

The nonsecret JSON format has fields `version`, `providers`, `evidence`, and
optionally `hosted_models`. `providers` maps `fatsecret`, `cofid` and (if selected)
`exa` to per-wire-attempt USD **upper bounds**, covering their endpoints, retries,
account tier and charges. Every included provider needs an evidence reference in
`evidence`; zero is accepted only as an explicit reviewed included/free price.
Missing fields/prices refuse. No guessed account prices are supplied here.

Each `hosted_models` key is the exact configured fallback model ID. Its value
must contain `version`, `source`, `max_context_tokens`, `input_per_million`,
`output_per_million`, and `tool_per_call_usd`. These must be verified full-context
limits and **worst applicable** rates, including long-context/cache/regional and
search-content charges. A source label is not automatic verification: the
independent live-run operator must approve the card. Hosted search is bounded to
one tool call; reservation covers two full contexts (before and after the tool),
the full output/reasoning cap and its tool fee. Other hosted tools, media,
stateful/previous-response calls and unknown request options refuse.

For tool-free Terra text extraction, the checked public short-context standard
list rates are $1 input/$5 output per million. The conservative reservation uses
$3.85 input/$5.50 output per million to cover both uncached input and the
documented maximum standard cache-write rate, plus the regional premium. It counts the entire UTF-8 serialized
request plus 4096 framing tokens, rejects bounds above 32000 input tokens, and
requires a positive output cap at most 2000. Fast/priority/unpriced tiers refuse.
These are price-based upper-bound reservations, **not billed spend**.

Hard ceilings cannot be increased through CLI: **300 model attempts, 200 lookup
attempts, US$10**. Every model attempt, including Responses extraction and hosted
fallback, shares the same budget. Lookups and transport retries also consume
counts and priced reservations. Failed calls and absent usage retain the full
reservation; reported absent usage/billed spend stays unknown, not zero. Once a
cap refuses, subsequent calls remain stopped. Cap-interrupted and configuration-
refused cases have no quality score and are excluded from completed denominators.
The runner uses an old-path stub, so it does not validate the full legacy coach.

## Behavior and evaluation changes

- Oversized previews are refused before a confirmable binding is persisted.
  The existing 1000-character Canvas reply cannot authorize hidden items/totals.
- Confirmation binds the displayed revision and session. A different session
  receives a fresh preview, not an unseen commit.
- Read errors retain unknown outcomes. Recovery reads are owner-scoped and
  independent of the eight-row/24-hour context window. Verified acknowledgments
  clear the pending intent durably; expired replay proof on an unresolved
  committed draft blocks safely rather than creating a new meal.
- Free-form component modifications have **no supported source-backed adapter in
  this pass**. Initial, revision-only and recalled modifications visibly ask the
  user to select an evidenced listed variant or remove the adjustment. They are
  never saved against an unchanged standard panel. A listed product variant is
  still resolved through the normal source evidence.
- Restricted/nested provider panels are omitted from persistent draft JSON.
  Attribution and retention metadata survive. Identity/consumed quantity remain
  available for refresh. A changed refreshed panel requires another preview and
  confirmation. Unknown retention policies conservatively prevent caching.
- Clarification scoring requires expected meaning and rejects error outcomes.
  Live portions compare gold consumed units against the actual source serving,
  not a scripted multiplier. Independent synthetic gold nutrition is checked
  against saved totals; receipt/row agreement alone is insufficient. Missing
  gold nutrition is explicitly not verified. The private original bench was not
  opened or relabeled; its earlier A/B scores are not recertified by this change.

## Public protocol/pricing references

- https://developers.openai.com/api/docs/models/gpt-6.1-terra
- https://developers.openai.com/api/docs/guides/structured-outputs

Public documentation confirms the identifier, strict-output support, reasoning
`none`, and stated pricing. It does not prove account availability or a live API
call succeeded.
