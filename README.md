# sdiff

Diffs two structured OpenAPI files and classifies every change as
**BREAKING**, **BEHAVIOUR**, or **COSMETIC**, with a one-line reason.

```
$ sdiff v1.yaml v2.yaml
  BREAKING  paths./users.get.parameters.limit.default   20 -> 100
            "paths./users.get.parameters.limit.default default changed 20 -> 100 (5x)"
  BEHAVIOUR components.schemas.User.required   [] -> ['email']
  COSMETIC  37 other changes
```

![demo](demo.gif)

Try it without installing anything locally beyond Python: [`demo.ipynb`](demo.ipynb)
runs the whole thing end to end against a bundled real example, prompts for
an API key if `.env` doesn't have one, and has a cell to drop in your own
spec pair.

## How it works — strict 3-stage pipeline

1. **Deterministic parse + align** (`sdiff.flatten`/`sdiff.diff`, no AI). Parses
   JSON or YAML, flattens into `{json-pointer-ish path: leaf value}`, and diffs
   the two flattened maps. Lists of objects that all have a `name` (OpenAPI
   `parameters`, `servers`, `tags`, ...) are aligned by name, not index, so
   inserting one parameter doesn't shift every sibling's path. Plain scalar
   lists (`required`, `enum`) compare as one sorted leaf.
2. **Classify each changed pair** — one request per pair to a `Judge`
   ([`JevJudge`](sdiff.py) by default, [`ChatJudge`](sdiff.py) as a drop-in
   swap via `--judge chat`). State sent is just `{path, old, new}` — nothing
   else — per Jev's own guidance that accuracy drops as irrelevant state
   grows. Jev answers a `Choice` over `{breaking, behaviour, cosmetic}`, a
   second `Choice` over a closed set of reason codes (`removed`,
   `added_required`, `type_changed`, ...), and three `Noul` cross-checks
   (`client_code_must_change`, `old_requests_still_valid`,
   `doc_or_example_only`) used in code to catch cases where the `kind`
   answer and the supporting nouls disagree — never to downgrade BREAKING,
   only to escalate or flag `?` for review.
3. **Deterministic report** — grouping, counts, the reason sentence (built
   from a template + the real values, in code — see below), exit code
   (non-zero if any BREAKING), `--json`.

## Why Jev never writes the reason sentence

Jev [doesn't generate text](https://docs.typesafe.ai) — it answers typed
questions against state. So the reason line isn't generated: Jev picks a
`reason` code from a fixed set of ~10, and `format_reason()` in `sdiff.py`
fills a template with the actual old/new values. Any arithmetic (e.g. the
`5x` above) is computed in code too — Jev's own docs say it "cannot reliably
perform calculations" and dates are "read as text not ordered quantities",
so counting, ratios, and date comparisons never touch the model.

## Swapping judges

```
sdiff v1.yaml v2.yaml --judge jev          # default, needs TYPESAFE_API_KEY
sdiff v1.yaml v2.yaml --judge chat         # cheap chat model fallback
```

`ChatJudge` talks to any OpenAI-compatible `/chat/completions` endpoint and
defaults to OpenRouter, so an existing `OPENROUTER_API_KEY` works with no
other setup:

- `OPENAI_BASE_URL` (default `https://openrouter.ai/api/v1`)
- `OPENROUTER_API_KEY` or `OPENAI_API_KEY`
- `--chat-model` / `SDIFF_CHAT_MODEL` (default `openai/gpt-5-nano`)

Both judges return the same `Verdict` shape, so the report, the reason
templates, and the cross-check logic are identical either way — swapping
judges is the one `--judge` flag, no other code path changes.

## Real example

Built from two real, adjacent releases of
[`stripe/openapi`](https://github.com/stripe/openapi) — `v2440` and `v2442`
(`openapi/spec3.json`), a bulk schema regeneration that both added a new
schema (`address_api_resource_terminal`) and changed an existing one's
required fields. A trimmed slice of both files (same schemas, unmodified
content) ships as `examples/stripe-v2440.yaml` / `examples/stripe-v2442.yaml`
so the example below is reproducible without downloading the full ~8MB spec.

```
$ sdiff examples/stripe-v2440.yaml examples/stripe-v2442.yaml --judge chat
  BREAKING  components.schemas.address_api_resource_terminal.properties.city.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.city.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.address_api_resource_terminal.properties.country.type  (absent) -> string
            "components.schemas.address_api_resource_terminal.properties.country.type was added (optional)"
  BREAKING  components.schemas.address_api_resource_terminal.properties.line1.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.line1.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.address_api_resource_terminal.properties.line2.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.line2.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.address_api_resource_terminal.properties.postal_code.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.postal_code.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.address_api_resource_terminal.properties.postal_code.nullable  (absent) -> True
            "components.schemas.address_api_resource_terminal.properties.postal_code.nullable constraint loosened: None -> True"
  BREAKING  components.schemas.address_api_resource_terminal.properties.state.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.state.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.checkout_us_bank_account_payment_method_options.properties.financial_connections.$ref  #/components/schemas/linked_account_options_common -> #/components/schemas/checkout_financial_connections_payment_method_options
            "components.schemas.checkout_us_bank_account_payment_method_options.properties.financial_connections.$ref type changed #/components/schemas/linked_account_options_common -> #/components/schemas/checkout_financial_connections_payment_method_options"
  BREAKING  components.schemas.connect_embedded_account_session_create_components.required  [...20 items...] -> [...21 items, +payment_method_settings...]
            "components.schemas.connect_embedded_account_session_create_components.required was added as required; requests without it now fail"
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.city.nullable  (absent) -> True
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.city.type  (absent) -> string
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.country.maxLength  (absent) -> 5000
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.country.nullable  (absent) -> True
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.line1.nullable  (absent) -> True
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.line2.nullable  (absent) -> True
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.line2.type  (absent) -> string
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.postal_code.type  (absent) -> string
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.state.nullable  (absent) -> True
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.state.type  (absent) -> string
  BEHAVIOUR components.schemas.connect_embedded_account_session_create_components.properties.payment_method_settings.$ref  (absent) -> #/components/schemas/connect_embedded_payment_method_settings_config_claim
  BEHAVIOUR components.schemas.connect_embedded_account_session_create_components.x-expandableFields  [...20 items...] -> [...21 items, +payment_method_settings...]
  BEHAVIOUR!components.schemas.event.title                 NotificationEvent -> Event
  COSMETIC  20 other changes
```

Exit code: `1` (a BREAKING change is present).

**Honest caveat, not swept under the rug:** this run used `--judge chat`
against `openai/gpt-5-nano` — the cheap fallback, not Jev (no
`TYPESAFE_API_KEY` was available in the sandbox this was built in). Look at
`.city.type`, `.line2.type`, `.postal_code.type`, `.state.type` (all
BEHAVIOUR) next to `.country.type` (BREAKING) — same schema, same kind of
leaf (`old=None`, a pure addition), different verdict. That's a live example
of Jev's own documented ["no structural invariance"](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)
failure mode, not an `sdiff` bug: a small model doesn't reliably give the
same answer to the same shape of question twice. It's exactly why the
Noul cross-checks and the `?`/`!` flags exist (see `event.title`, flagged
`!` — cosmetic-looking title casing that `client_code_must_change` scored
high enough to escalate to BEHAVIOUR). Re-run with `--judge jev` for the
purpose-built model this tool is designed around, once you have a
`TYPESAFE_API_KEY`.

## Limitations (v0.1)

- **No rename detection.** A renamed field or parameter shows as one removal
  + one addition, not a rename. Out of scope for v0.1.
- **Non-`name`-keyed lists of objects are index-aligned** — inserting or
  reordering an item in e.g. a raw JSON array of objects with no `name` key
  can misreport as N changes instead of one. Marked with a `# ponytail:`
  comment in `sdiff.py` (`flatten()`); add id-based alignment if a real spec
  hits this.
- **One format**: OpenAPI/JSON/YAML, aligned by structural path. Nothing
  format-specific beyond that — no separate `--format=json-schema` flag,
  since a JSON Schema file is already just JSON the flattener eats directly.
- **State is trusted, spec content is not.** A spec can carry injected
  instructions. A judgement here only ever produces printed text and the
  exit code — no side effect is ever triggered directly from a Jev/chat
  answer.

## Install & run

```
pip install -e .[dev]
export TYPESAFE_API_KEY=...        # or OPENROUTER_API_KEY for --judge chat
python sdiff.py old.yaml new.yaml
pytest -q                          # one test file, no network
```
