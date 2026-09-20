#!/usr/bin/env python3
"""sdiff: diff two structured (OpenAPI) files and classify every change as
BREAKING, BEHAVIOUR, or COSMETIC using a Judge backed by Jev (default) or a
cheap chat model.

Architecture is a strict 3-stage pipeline:
  1. parse + align (flatten/diff)      -- deterministic, no AI
  2. classify each changed pair        -- one Judge call per pair
  3. report + exit code                -- deterministic, no AI

Jev never generates the reason sentence: it picks a typed `reason` code from
a closed set, and the sentence is templated here in code from the real old/
new values (see REASON_TEMPLATES). Any arithmetic (e.g. "5x") is computed
here too, never by the model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple

import yaml


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=value per line, no interpolation, existing
    env vars win. # ponytail: no quoting/multiline support, add python-dotenv
    if that's ever needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

KINDS = ("breaking", "behaviour", "cosmetic")

# Shared vocabulary for both judges, so Jev and the chat fallback are asked
# the exact same question in substance.
STATE_NOTE = (
    "old=None means this path did not exist before (a pure addition). "
    "new=None means it was removed. Adding a new OPTIONAL field, parameter, "
    "or schema is not breaking on its own."
)

KIND_CRITERIA = {
    "breaking": "Old client requests or responses would fail or behave "
                "incorrectly against the new spec. " + STATE_NOTE,
    "behaviour": "Requests still work, but data or behavior returned or "
                 "accepted differs in a way callers may notice. " + STATE_NOTE,
    "cosmetic": "Wording, description, or ordering only; no functional "
                "effect on requests or responses. " + STATE_NOTE,
}

REASON_CRITERIA = {
    "removed": "The field, parameter, or path was removed entirely (new=None).",
    "added_optional": "A new OPTIONAL field, parameter, or schema was added (old=None, not in `required`).",
    "added_required": "A new REQUIRED field or parameter was added (old=None, in `required`).",
    "optional_to_required": "An existing field or parameter became required.",
    "type_changed": "The data type of an EXISTING field changed (e.g. string -> integer). Not for old=None.",
    "enum_narrowed": "The set of allowed values shrank.",
    "default_changed": "A default value changed.",
    "constraint_tightened": "A limit on an EXISTING field got stricter (e.g. max lowered, min raised). Not for old=None.",
    "constraint_loosened": "A limit on an EXISTING field got looser (e.g. max raised, min lowered).",
    "doc_only": "Only a description, summary, example, or comment changed.",
    "other": "None of the above describes it well.",
}

REASON_TEMPLATES = {
    "removed": "{path} was removed",
    "added_optional": "{path} was added (optional)",
    "added_required": "{path} was added as required; requests without it now fail",
    "optional_to_required": "{path} changed optional -> required",
    "type_changed": "{path} type changed {old} -> {new}",
    "enum_narrowed": "{path} allowed values narrowed: {old} -> {new}",
    "default_changed": "{path} default changed {old} -> {new}",
    "constraint_tightened": "{path} constraint tightened: {old} -> {new}",
    "constraint_loosened": "{path} constraint loosened: {old} -> {new}",
    "doc_only": "{path} documentation only, no functional change",
    "other": "{path} changed {old} -> {new}",
}

NOUL_KEYS = ("client_code_must_change", "old_requests_still_valid", "doc_or_example_only")


# ---------------------------------------------------------------------------
# 1. Deterministic parse + align
# ---------------------------------------------------------------------------

def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f) if path.endswith(".json") else yaml.safe_load(f)


def flatten(obj, prefix: str = "") -> dict:
    """Flatten nested dict/list structure into {dotted_path: leaf_value}.

    Lists of dicts that all carry a `name` key (OpenAPI parameters, servers,
    tags, ...) are aligned by name instead of index, so inserting one
    parameter doesn't shift every sibling's path. Lists of plain scalars
    (`required`, `enum`, ...) are compared as one sorted leaf so reordering
    isn't reported as a change. Other lists of dicts fall back to index
    alignment.
    # ponytail: index-aligned lists misreport insert/remove/reorder as N
    # changes; add id-based alignment (like `name`) if a real spec hits this.
    """
    leaves: dict = {}

    def walk(o, path):
        if isinstance(o, dict):
            if not o:
                leaves[path or "."] = o
                return
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(o, list):
            if not o:
                leaves[path or "."] = o
                return
            if all(isinstance(i, dict) and "name" in i for i in o):
                for item in o:
                    walk(item, f"{path}.{item['name']}" if path else str(item["name"]))
            elif all(not isinstance(i, (dict, list)) for i in o):
                leaves[path] = sorted(o, key=str)
            else:
                for idx, item in enumerate(o):
                    walk(item, f"{path}[{idx}]")
        else:
            leaves[path] = o

    walk(obj, prefix)
    return leaves


def diff(a, b) -> list[tuple[str, object, object]]:
    """Return [(path, old, new)] for every leaf that changed, was added, or
    was removed. Missing side is None."""
    fa, fb = flatten(a), flatten(b)
    changed = []
    for path in sorted(set(fa) | set(fb)):
        old, new = fa.get(path), fb.get(path)
        if old != new:
            changed.append((path, old, new))
    return changed


# ---------------------------------------------------------------------------
# 2. Judge
# ---------------------------------------------------------------------------

class Verdict(NamedTuple):
    kind: str
    reason: str
    confidence: float
    nouls: dict


def format_reason(reason: str, path: str, old, new) -> str:
    text = REASON_TEMPLATES.get(reason, REASON_TEMPLATES["other"]).format(
        path=path, old=old, new=new
    )
    try:
        o, n = float(old), float(new)
        if o != 0 and o != n:
            ratio = n / o
            text += f" ({ratio:.3g}x)" if ratio >= 1 else f" ({1 / ratio:.3g}x smaller)"
    except (TypeError, ValueError):
        pass
    return text


def apply_crosscheck(v: Verdict) -> tuple[str, str]:
    """Code-side correction for Jev's documented lack of structural
    invariance between the `kind` choice and the supporting nouls. Never
    downgrades BREAKING; only escalates or flags for review."""
    kind, flag = v.kind, ""
    if kind == "cosmetic" and v.nouls.get("client_code_must_change", 0) > 0.8:
        kind, flag = "behaviour", "!"
    elif kind == "breaking" and v.nouls.get("doc_or_example_only", 0) > 0.9:
        flag = "?"
    if v.confidence < 0.5 and not flag:
        flag = "?"
    return kind, flag


class JevJudge:
    """Backed by TypeSafe's Jev model. One state = one changed pair."""

    def __init__(self, model: str | None = None):
        from typesafe_sdk import TypeSafeClient

        self._client = TypeSafeClient()
        self._model = model

    def judge(self, path: str, old, new) -> Verdict:
        from typesafe_sdk import Choice, Noul

        kwargs = {} if self._model is None else {"model": self._model}
        result = self._client.system_one(
            state={"path": path, "old": old, "new": new},
            questions={
                "kind": Choice(
                    instructions="Classify this OpenAPI spec change for API consumers.",
                    criteria=KIND_CRITERIA,
                ),
                "reason": Choice(
                    instructions="What specifically changed.",
                    criteria=REASON_CRITERIA,
                ),
                "client_code_must_change": Noul(
                    instructions="Would existing client code need to change to keep working?"
                ),
                "old_requests_still_valid": Noul(
                    instructions="Would requests valid under the old spec still be valid under the new spec?"
                ),
                "doc_or_example_only": Noul(
                    instructions="Is this change limited to documentation, descriptions, or examples, with no effect on requests or responses?"
                ),
            },
            **kwargs,
        )
        kind_ans = result.choices["kind"]
        reason_ans = result.choices["reason"]
        nouls = {k: result.nouls[k].noul for k in NOUL_KEYS}
        return Verdict(kind_ans.choice, reason_ans.choice, kind_ans.confidence, nouls)


_CHAT_SYSTEM_PROMPT = (
    "You classify a single OpenAPI spec change. Respond with ONLY a JSON object "
    "with these keys:\n"
    f'- "kind": one of {list(KIND_CRITERIA)} ' + json.dumps(KIND_CRITERIA) + "\n"
    f'- "reason": one of {list(REASON_CRITERIA)} ' + json.dumps(REASON_CRITERIA) + "\n"
    '- "confidence": number 0-1\n'
    f"- {NOUL_KEYS}: each a number 0-1 answering the matching yes/no question:\n"
    "  client_code_must_change: would existing client code need to change to keep working?\n"
    "  old_requests_still_valid: would requests valid under the old spec still be valid under the new spec?\n"
    "  doc_or_example_only: is this change limited to documentation/examples with no functional effect?\n"
    "No arithmetic, no prose reasoning in the output. JSON only."
)


class ChatJudge:
    """Backed by any OpenAI-compatible chat endpoint. Defaults to OpenRouter
    so an existing OPENROUTER_API_KEY works with no other config."""

    def __init__(self, model: str | None = None):
        import requests

        self._requests = requests
        self._base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self._api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise RuntimeError("Set OPENROUTER_API_KEY or OPENAI_API_KEY for --judge chat")
        self._model = model or os.environ.get("SDIFF_CHAT_MODEL", "openai/gpt-5-nano")

    def judge(self, path: str, old, new) -> Verdict:
        resp = self._requests.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"path": path, "old": old, "new": new}, default=str)},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        nouls = {k: float(data.get(k, 0.5)) for k in NOUL_KEYS}
        kind = data.get("kind", "cosmetic")
        if kind not in KIND_CRITERIA:
            kind = "cosmetic"
        return Verdict(kind, data.get("reason", "other"), float(data.get("confidence", 0.5)), nouls)


JUDGES = {"jev": JevJudge, "chat": ChatJudge}


# ---------------------------------------------------------------------------
# classify + report
# ---------------------------------------------------------------------------

def classify_pair(judge, path: str, old, new) -> dict:
    v = judge.judge(path, old, new)
    kind, flag = apply_crosscheck(v)
    return {
        "path": path,
        "old": old,
        "new": new,
        "kind": kind,
        "flag": flag,
        "reason": format_reason(v.reason, path, old, new),
        "confidence": v.confidence,
        "nouls": v.nouls,
    }


def run(old_obj, new_obj, judge, max_workers: int = 16) -> list[dict]:
    changes = diff(old_obj, new_obj)
    results: list[dict | None] = [None] * len(changes)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(classify_pair, judge, p, o, n): i for i, (p, o, n) in enumerate(changes)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    results.sort(key=lambda r: r["path"])
    return results


def _value_str(v) -> str:
    return "(absent)" if v is None else str(v)


def render(results: list[dict], json_output: bool) -> None:
    if json_output:
        print(json.dumps(results, indent=2, default=str))
        return

    breaking = [r for r in results if r["kind"] == "breaking"]
    behaviour = [r for r in results if r["kind"] == "behaviour"]
    cosmetic = [r for r in results if r["kind"] == "cosmetic"]

    for r in breaking:
        label = f"BREAKING{r['flag']}"
        print(f"  {label:<10}{r['path']:<45}  {_value_str(r['old'])} -> {_value_str(r['new'])}")
        print(f'            "{r["reason"]}"')
    for r in behaviour:
        label = f"BEHAVIOUR{r['flag']}"
        print(f"  {label:<10}{r['path']:<45}  {_value_str(r['old'])} -> {_value_str(r['new'])}")
    if cosmetic:
        print(f"  COSMETIC  {len(cosmetic)} other changes")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff two OpenAPI files and classify each change.")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--judge", choices=list(JUDGES), default="jev")
    p.add_argument("--chat-model", default=None, help="override model for --judge chat")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        old_obj, new_obj = load(args.old), load(args.new)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        judge = JevJudge() if args.judge == "jev" else ChatJudge(model=args.chat_model)
        results = run(old_obj, new_obj, judge)
    except Exception as e:  # noqa: BLE001 -- surface any judge/SDK failure plainly
        print(f"error: {e}", file=sys.stderr)
        return 2

    render(results, args.json)
    return 1 if any(r["kind"] == "breaking" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
