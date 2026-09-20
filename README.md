# sdiff

Diffs two structured files and tells you whether each change is BREAKING,
BEHAVIOUR, or just noise.

```
$ sdiff v1.yaml v2.yaml
  BREAKING  paths./users.get.parameters.limit.default   20 -> 100
            "paths./users.get.parameters.limit.default default changed 20 -> 100 (5x)"
  BEHAVIOUR components.schemas.User.required   [] -> ['email']
  COSMETIC  37 other changes
```

`git diff` on a spec, manifest, or config file just shows you text. It won't
tell you that bumping a default page size from 20 to 100 means every existing
client suddenly pulls 5x the data. sdiff parses both files, aligns the real
structure, and asks [Jev](https://docs.typesafe.ai) to classify each change so
you don't have to read a five-thousand-line diff by hand.

## How it works

1. **Parse and align** — plain code, no model involved. Flatten both files
   into paths, align lists of objects (`parameters`, `containers`, ...) by an
   id-like key instead of index so inserting one item doesn't shift every
   sibling. Diff the leaves.
2. **Classify each changed pair** — a deterministic rule prefilter resolves
   the obvious noise (doc/comment-only changes) for free; everything else
   gets one Jev call per pair, nothing batched, with identical leaf changes
   (e.g. the same reworded description across 200 schemas) classified once
   and reused. It picks breaking/behaviour/cosmetic, picks a reason code, and
   answers a few yes/no cross-checks we use to catch it contradicting itself.
   Those checks can escalate a verdict or flag it for review, never downgrade
   a BREAKING.
3. **Report** — grouped output, exit code non-zero if anything's BREAKING,
   `--json` for scripting.

Jev can't generate text, so it never writes the reason sentence — it just
picks a code from a fixed list, and the sentence gets built from a template
in code using the real values. Same story for arithmetic: Jev is bad at
counting and math, so any ratio or count happens in code, never in the
model.

## Profiles: what "breaking" means for your format

What counts as BREAKING is format-specific — the engine (flatten, diff,
prefilter, cache, report) has no domain knowledge at all; every bit of that
knowledge lives in a **profile**, a small YAML file. sdiff ships eight:

| profile | audience | BREAKING means |
|---|---|---|
| `openapi` | API consumers | removed field, new required parameter, tightened constraint |
| `k8s` | a workload's operators | bad image tag, broken probe, missing required env var (labels/annotations are COSMETIC) |
| `json-schema` | data producers/validators | new required property, narrowed enum, tightened type or constraint |
| `dependency-manifest` | the build | removed dependency, major version bump, raised engine floor |
| `ci-workflow` | the pipeline | removed job/required check, narrowed trigger; permissions widened is BEHAVIOUR |
| `iam-policy` | security reviewers | access removed **or** access widened (wildcard action/resource, new principal) — both fail the check |
| `config` | the running application | missing required setting (a comment is COSMETIC) |
| `generic` | the fallback for anything else | structural change with no format-specific reading |

The right profile is auto-detected — file **contents** first (a distinctive
top-level key like `openapi:`, `apiVersion:`/`kind:`, `$schema`,
`dependencies:`), filename only as a fallback when nothing in the content
matches — or pick one explicitly:

```
sdiff old.yaml new.yaml --profile k8s
sdiff --list-profiles
```

When the profile wasn't given explicitly, sdiff prints which one it picked to
stderr (`# profile: k8s (auto-detected)`), since a silent wrong guess is the
main failure mode once several profiles' content keys could plausibly
overlap.

Point `--profile` at your own YAML file to add a new domain with no code
change — see `sdiff/profiles/openapi.yaml` for the format: what BREAKING /
BEHAVIOUR / COSMETIC mean here, the reason vocabulary, which keys align lists
by identity, and an optional list of `rules` that skip the judge entirely.
Each rule is either a static glob that can only assert COSMETIC (e.g.
`*.description`), or a glob paired with a named `comparator` (currently just
`semver`) that can return any verdict from real code — e.g.
`dependency-manifest` resolves a major version bump to BREAKING without ever
calling the judge. Every built-in profile is covered by a conformance test in
`test_sdiff.py`, and a custom profile should be too before you rely on it.

## Judges

```
sdiff v1.yaml v2.yaml --judge jev     # default, needs TYPESAFE_API_KEY
sdiff v1.yaml v2.yaml --judge chat    # cheap fallback, needs OPENROUTER_API_KEY
sdiff v1.yaml v2.yaml --offline       # no model, no network: rules only
```

`ChatJudge` talks to any OpenAI-compatible endpoint and defaults to
OpenRouter, so an existing `OPENROUTER_API_KEY` just works. Override the
endpoint or model with `OPENAI_BASE_URL` or `--chat-model`. Both judges return
the same shape, so swapping is the one flag — nothing else in the code path
changes.

`--offline` skips the judge entirely. Only the profile's deterministic rules
resolve anything; everything else comes back `UNREVIEWED`, never silently
folded into COSMETIC, so exit code 0 never means "nothing to look at" — it
means "nothing the rules found, and you didn't ask a judge either."

## Speed

The model call is the expensive part, so sdiff avoids making it when it can:

- **Rule prefilter** — a profile's `rules` resolve obviously-cosmetic changes
  (doc/comment/label fields) with zero network calls.
- **Dedupe cache** — leaf changes that are structurally identical (same leaf
  name, same old value, same new value — e.g. one description reworded
  across 200 schemas) are classified once per run and reused everywhere else
  they occur.
- **`--offline`** for a rules-only pass with no API key at all.
- **`--workers N`** to tune judge-call concurrency (default 16).

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
- Lists of objects without an id-like key (see a profile's `align_keys`) are
  aligned by index, so inserting or reordering one item can misreport as
  several changes.
- JSON, YAML, or TOML input; the domain vocabulary is a profile, not a parser
  — a new file format still needs a `load()` case.
- `openapi`'s criteria blend the API-client and API-server perspectives (a
  removed response field breaks clients, a new required request field breaks
  callers, and both are just BREAKING today). A real split would need the
  file to say which side it's diffed from, which it doesn't — noted, not
  fixed.
- Spec content is untrusted input. A verdict only ever produces printed
  text and an exit code — nothing it says triggers a side effect directly.

## CI

`.github/workflows/test.yml` runs `pytest -q` on push and PR. No live judge
runs in CI — the judge is non-deterministic and needs a network call and an
API key, which makes it a bad fit for a merge gate. `sdiff` itself is meant
to be run in *your* CI against *your* spec/manifest/policy changes; this
repo's own CI just proves the deterministic half (parse/align/diff/rules)
works.

## Install

```
pip install -e .[dev]
cp .env.example .env        # add TYPESAFE_API_KEY or OPENROUTER_API_KEY
sdiff old.yaml new.yaml      # or: python -m sdiff old.yaml new.yaml
pytest -q                   # no network needed
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
