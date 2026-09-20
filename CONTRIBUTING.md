# Contributing

This is a small tool and it should stay small. Before adding something,
check whether it actually needs to exist.

## Setup

```
pip install -e .[dev]
pytest -q
```

No network needed for the tests — `test_sdiff.py` uses a fake judge.

## Before opening a PR

- If you touch `flatten()`, `diff()`, or the cross-check logic, add or
  update an assertion in `test_sdiff.py`. It's one file, keep it that way.
- New reason codes, kind criteria, or domain vocabulary for a format go in a
  profile YAML under `sdiff/profiles/`, not in Python. Both judges read the
  same profile, so wording changes there apply to Jev and the chat fallback
  identically — never special-case one judge.
- The three kind names (`breaking`/`behaviour`/`cosmetic`) are fixed across
  every profile — `--json` consumers and CI checks depend on them. A profile
  may reword what each one *means*, never rename it.
- A new profile must ship with a `detect.keys` entry wherever the format has
  a distinctive top-level key — content is checked before filename, and
  filename globs are guesses that lose to any profile's content match. Add
  `detect.files` too as a fallback, but don't rely on it alone.
- A static `rules` glob may only assert `cosmetic` — the prefilter exists to
  skip obviously-noise calls, not to replace the judge. A rule that needs to
  assert anything else must pair its glob with a `comparator`: a named,
  deterministic function in the `COMPARATORS` registry (`sdiff/__init__.py`)
  that gets `(old, new)` and returns a verdict or `None` (fall through to the
  judge — a comparator that can't parse its input must never guess). Add a
  new comparator only for something that's genuinely arithmetic or
  string-format logic, like `semver`; anything judgment-shaped stays with the
  judge.
- Every built-in profile must pass the conformance test in `test_sdiff.py`
  (parametrized over `BUILTIN_PROFILES`) — it's the rot guard for a growing
  catalogue: valid kinds/nouls, every reason has both `criteria` and
  `template`, every rule names a real reason or a real comparator, no static
  rule asserts non-cosmetic. Add your profile to `BUILTIN_PROFILES` and this
  test covers it for free; don't write a bespoke per-profile test.
- The `Judge` interface has exactly two implementations on purpose. If you
  want a different model behind `ChatJudge`, point `OPENAI_BASE_URL` at it
  rather than writing a new class.
- No new dependencies unless the standard library genuinely can't do it.
- Bug fixes should go to the root cause, not the symptom that got reported.
- Run `pytest -q` first. CI just runs the same suite, no network.

## Scope

Rename detection and fuzzy alignment beyond a profile's `align_keys` are
known gaps, not bugs — see the README's "Known limits" section. Open an
issue if you've got a real file that needs it before building it.
