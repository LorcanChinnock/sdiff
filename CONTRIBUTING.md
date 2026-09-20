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
- New reason codes or kind categories go in the shared `REASON_CRITERIA` /
  `KIND_CRITERIA` dicts in `sdiff.py`, so Jev and the chat fallback both see
  the same vocabulary. Don't special-case one judge.
- The `Judge` interface has exactly two implementations on purpose. If you
  want a different model behind `ChatJudge`, point `OPENAI_BASE_URL` at it
  rather than writing a new class.
- No new dependencies unless the standard library genuinely can't do it.
- Bug fixes should go to the root cause, not the symptom that got reported.
- Run `pytest -q` first. If your change touches the `examples/` files, the
  `sdiff-check` CI job will diff them against `main` and fail the check if
  it finds a BREAKING change.

## Scope

Rename detection and fuzzy alignment across formats other than OpenAPI are
known gaps, not bugs — see the README's "Known limits" section. Open an
issue if you've got a real spec that needs it before building it.
