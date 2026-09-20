# sdiff

Diffs two OpenAPI files and tells you whether each change is BREAKING,
BEHAVIOUR, or just noise.

```
$ sdiff v1.yaml v2.yaml
  BREAKING  paths./users.get.parameters.limit.default   20 -> 100
            "paths./users.get.parameters.limit.default default changed 20 -> 100 (5x)"
  BEHAVIOUR components.schemas.User.required   [] -> ['email']
  COSMETIC  37 other changes
```

`git diff` on a spec just shows you text. It won't tell you that bumping a
default page size from 20 to 100 means every existing client suddenly pulls
5x the data. sdiff parses both files, aligns the real structure, and asks
[Jev](https://docs.typesafe.ai) to classify each change so you don't have to
read a five-thousand-line spec diff by hand.

## How it works

1. **Parse and align** — plain code, no model involved. Flatten both files
   into paths, align OpenAPI lists (`parameters`, `servers`, `tags`, ...) by
   their `name` instead of index so inserting one item doesn't shift every
   sibling. Diff the leaves.
2. **Classify each changed pair** — one Jev call per pair, nothing batched.
   It picks breaking/behaviour/cosmetic, picks a reason code, and answers a
   few yes/no cross-checks we use to catch it contradicting itself. Those
   checks can escalate a verdict or flag it for review, never downgrade a
   BREAKING.
3. **Report** — grouped output, exit code non-zero if anything's BREAKING,
   `--json` for scripting.

Jev can't generate text, so it never writes the reason sentence — it just
picks a code from a fixed list, and the sentence gets built from a template
in code using the real values. Same story for arithmetic: Jev is bad at
counting and math, so any ratio or count happens in code, never in the
model.

## Judges

```
sdiff v1.yaml v2.yaml --judge jev     # default, needs TYPESAFE_API_KEY
sdiff v1.yaml v2.yaml --judge chat    # cheap fallback, needs OPENROUTER_API_KEY
```

`ChatJudge` talks to any OpenAI-compatible endpoint and defaults to
OpenRouter, so an existing `OPENROUTER_API_KEY` just works. Override the
endpoint or model with `OPENAI_BASE_URL` or `--chat-model`. Both judges return
the same shape, so swapping is the one flag — nothing else in the code path
changes.

## A real example

`examples/` has a trimmed slice of two real, adjacent
[stripe/openapi](https://github.com/stripe/openapi) releases (`v2440` and
`v2442`), a schema regeneration that added a new object and tightened a
required list on an existing one:

```
$ sdiff examples/stripe-v2440.yaml examples/stripe-v2442.yaml --judge chat
  BREAKING  components.schemas.address_api_resource_terminal.properties.city.maxLength  (absent) -> 5000
            "components.schemas.address_api_resource_terminal.properties.city.maxLength constraint tightened: None -> 5000"
  BREAKING  components.schemas.connect_embedded_account_session_create_components.required  [...20 items...] -> [...21 items, +payment_method_settings...]
            "components.schemas.connect_embedded_account_session_create_components.required was added as required; requests without it now fail"
  BEHAVIOUR components.schemas.address_api_resource_terminal.properties.city.nullable  (absent) -> True
  BEHAVIOUR!components.schemas.event.title                 NotificationEvent -> Event
  COSMETIC  20 other changes
```
exit code 1

That run used `--judge chat` on `gpt-5-nano`, not Jev — worth knowing since
the small model isn't perfectly consistent (a couple of near-identical leaf
additions get classified differently from each other in the full output).
That's the kind of thing the `?`/`!` flags and the cross-checks exist for.
Run it with `--judge jev` for the model this tool's actually built around.

## Known limits

- No rename detection — a renamed field shows up as a remove + an add.
- Lists of objects without a `name` field are aligned by index, so
  inserting or reordering one item can misreport as several changes.
- One format for now: OpenAPI as JSON or YAML.
- Spec content is untrusted input. A verdict only ever produces printed
  text and an exit code — nothing it says triggers a side effect directly.

## CI

`.github/workflows/sdiff-check.yml` runs this on its own PRs: any time a PR
touches a file under `examples/`, it diffs that file against the base
branch and fails the check on a BREAKING result.

## Install

```
pip install -e .[dev]
cp .env.example .env        # add TYPESAFE_API_KEY or OPENROUTER_API_KEY
python sdiff.py old.yaml new.yaml
pytest -q                   # no network needed
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
