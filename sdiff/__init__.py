#!/usr/bin/env python3
"""sdiff: diff two structured files and classify every change as BREAKING,
BEHAVIOUR, or COSMETIC using a Judge backed by Jev (default) or a cheap chat
model.

Domain vocabulary (what "breaking" means, what reasons exist, how lists
align) lives in a profile -- a YAML file under sdiff/profiles/ or a path the
user points at, picked by --profile or auto-detected from the input. The
engine itself (flatten/diff/run/render) has no domain knowledge.

Architecture is a strict 3-stage pipeline:
  1. parse + align (flatten/diff)      -- deterministic, no AI
  2. classify each changed pair        -- one Judge call per pair, with a
                                           deterministic rule prefilter and
                                           a dedupe cache in front of it
  3. report + exit code                -- deterministic, no AI

Jev never generates the reason sentence: it picks a typed `reason` code from
a closed set, and the sentence is templated here in code from the real old/
new values (see each profile's `reasons.*.template`). Any arithmetic (e.g.
"5x") is computed here too, never by the model.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import resources
from typing import NamedTuple

import tomllib
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

BUILTIN_PROFILES = (
    "openapi", "k8s", "json-schema", "dependency-manifest",
    "ci-workflow", "iam-policy", "config", "generic",
)

_REQUIRED_PROFILE_KEYS = (
    "name", "subject", "audience", "align_keys", "state_note",
    "kinds", "reasons", "nouls",
)
_REQUIRED_NOULS = ("client_code_must_change", "old_requests_still_valid", "doc_or_example_only")

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _semver_compare(old, new) -> tuple[str, str] | None:
    """Deterministic version-range comparator for a `comparator: semver`
    rule. Extracts the first X.Y.Z triple out of each side (tolerating a
    leading ^/~/>=/>/=/v and trailing range junk like `workspace:*` --
    anything without a clean triple on both sides returns None, which falls
    through to the judge rather than guessing)."""
    om, nm = _SEMVER_RE.search(str(old) or ""), _SEMVER_RE.search(str(new) or "")
    if not om or not nm:
        return None
    (o_maj, o_min, _), (n_maj, n_min, _) = (om.groups(), nm.groups())
    if o_maj != n_maj:
        return "breaking", "type_changed"
    if o_min != n_min:
        return "behaviour", "constraint_loosened"
    return "cosmetic", "doc_only"


# (old, new) -> (kind, reason) | None ; None means "no opinion, ask the judge".
COMPARATORS = {"semver": _semver_compare}


# ---------------------------------------------------------------------------
# Profiles: domain vocabulary as data. The engine below (flatten/diff/run/
# render) has no domain knowledge -- everything OpenAPI/k8s/config-specific
# lives in sdiff/profiles/*.yaml or a user-supplied YAML file.
# ---------------------------------------------------------------------------

def _read_builtin_profile(name: str) -> dict:
    text = resources.files("sdiff.profiles").joinpath(f"{name}.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def validate_profile(profile: dict, source: str) -> None:
    missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in profile]
    if missing:
        raise ValueError(f"profile {source!r} missing required key(s): {', '.join(missing)}")
    if set(profile["kinds"]) != set(KINDS):
        raise ValueError(f"profile {source!r} kinds must be exactly {KINDS}, got {tuple(profile['kinds'])}")
    missing_nouls = [k for k in _REQUIRED_NOULS if k not in profile["nouls"]]
    if missing_nouls:
        raise ValueError(f"profile {source!r} missing required noul(s): {', '.join(missing_nouls)}")
    for k, v in profile["reasons"].items():
        if "criteria" not in v or "template" not in v:
            raise ValueError(f"profile {source!r} reason {k!r} needs both criteria and template")
    for rule in profile.get("rules", []):
        glob = rule.get("glob")
        has_kind, has_comparator = "kind" in rule, "comparator" in rule
        if has_kind == has_comparator:  # both or neither
            raise ValueError(f"profile {source!r} rule {glob!r} must set exactly one of kind/comparator")
        if has_kind:
            if rule["kind"] != KINDS[2]:
                raise ValueError(f"profile {source!r} static rule {glob!r} may only assert {KINDS[2]!r} "
                                  f"(got {rule['kind']!r}); use a comparator for any other verdict")
            if rule["reason"] not in profile["reasons"]:
                raise ValueError(f"profile {source!r} rule {glob!r} references unknown reason {rule['reason']!r}")
        elif rule["comparator"] not in COMPARATORS:
            raise ValueError(f"profile {source!r} rule {glob!r} references unknown comparator {rule['comparator']!r}")


def _matches_file(path: str, pattern: str) -> bool:
    """A bare filename pattern (`values.yaml`) should match regardless of
    what directory the file lives in; a pattern with a path component
    (`.github/workflows/*.yml`) should match the path's tail. Try both,
    normalizing \\ to / for Windows."""
    path = path.replace("\\", "/")
    return fnmatch.fnmatch(os.path.basename(path), pattern) or fnmatch.fnmatch(path, f"*{pattern}")


def _detect_profile_name(old_obj, new_obj, old_path: str, new_path: str) -> str:
    named = [(name, _read_builtin_profile(name).get("detect", {}))
             for name in BUILTIN_PROFILES if name != "generic"]
    for name, detect in named:  # pass 1: content is evidence
        keys = detect.get("keys", [])
        if keys and any(isinstance(o, dict) and any(k in o for k in keys) for o in (old_obj, new_obj)):
            return name
    for name, detect in named:  # pass 2: filename is only a guess
        for pattern in detect.get("files", []):
            if _matches_file(old_path, pattern) or _matches_file(new_path, pattern):
                return name
    return "generic"


def load_profile(spec: str | None, old_obj=None, new_obj=None, old_path: str = "", new_path: str = "") -> dict:
    """Resolve --profile into a validated profile dict.

    `spec` is a built-in name, a path to a YAML file, or None to auto-detect
    from the input documents' top-level keys and the input filenames.
    """
    if spec is None:
        spec = _detect_profile_name(old_obj, new_obj, old_path, new_path)
    if spec in BUILTIN_PROFILES:
        profile = _read_builtin_profile(spec)
    else:
        with open(spec, encoding="utf-8") as f:
            profile = yaml.safe_load(f)
    validate_profile(profile, spec)
    return profile


# ---------------------------------------------------------------------------
# 1. Deterministic parse + align
# ---------------------------------------------------------------------------

def load(path: str):
    if path.endswith(".toml"):
        with open(path, "rb") as f:
            return tomllib.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f) if path.endswith(".json") else yaml.safe_load(f)


def flatten(obj, prefix: str = "", align_keys: tuple[str, ...] = ("name",)) -> dict:
    """Flatten nested dict/list structure into {dotted_path: leaf_value}.

    Lists of dicts that all carry one of `align_keys` (e.g. OpenAPI
    `parameters`/`servers`/`tags` use `name`, k8s manifests often use `id` or
    `key`) are aligned by that key instead of index, so inserting one item
    doesn't shift every sibling's path. Lists of plain scalars (`required`,
    `enum`, ...) are compared as one sorted leaf so reordering isn't reported
    as a change. Other lists of dicts fall back to index alignment.
    # ponytail: index-aligned lists misreport insert/remove/reorder as N
    # changes; add id-based alignment (like `name`) via a profile's
    # align_keys if a real input hits this.
    """
    leaves: dict = {}

    def align_key_for(items) -> str | None:
        for key in align_keys:
            if all(isinstance(i, dict) and key in i for i in items):
                return key
        return None

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
            key = align_key_for(o)
            if key is not None:
                for item in o:
                    walk(item, f"{path}.{item[key]}" if path else str(item[key]))
            elif all(not isinstance(i, (dict, list)) for i in o):
                leaves[path] = sorted(o, key=str)
            else:
                for idx, item in enumerate(o):
                    walk(item, f"{path}[{idx}]")
        else:
            leaves[path] = o

    walk(obj, prefix)
    return leaves


def diff(a, b, align_keys: tuple[str, ...] = ("name",)) -> list[tuple[str, object, object]]:
    """Return [(path, old, new)] for every leaf that changed, was added, or
    was removed. Missing side is None."""
    fa, fb = flatten(a, align_keys=align_keys), flatten(b, align_keys=align_keys)
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


def format_reason(profile: dict, reason: str, path: str, old, new) -> str:
    templates = {k: v["template"] for k, v in profile["reasons"].items()}
    text = templates.get(reason, templates["other"]).format(path=path, old=old, new=new)
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
    downgrades BREAKING; only escalates or flags for review. Kind names are
    fixed across all profiles (see KINDS), so this stays profile-agnostic."""
    kind, flag = v.kind, ""
    if kind == KINDS[2] and v.nouls.get("client_code_must_change", 0) > 0.8:
        kind, flag = KINDS[1], "!"
    elif kind == KINDS[0] and v.nouls.get("doc_or_example_only", 0) > 0.9:
        flag = "?"
    if v.confidence < 0.5 and not flag:
        flag = "?"
    return kind, flag


class JevJudge:
    """Backed by TypeSafe's Jev model. One state = one changed pair."""

    def __init__(self, profile: dict, model: str | None = None):
        from typesafe_sdk import TypeSafeClient

        self._client = TypeSafeClient()
        self._profile = profile
        self._model = model

    def judge(self, path: str, old, new) -> Verdict:
        from typesafe_sdk import Choice, Noul

        p = self._profile
        kind_criteria = {k: v + " " + p["state_note"] for k, v in p["kinds"].items()}
        reason_criteria = {k: v["criteria"] for k, v in p["reasons"].items()}
        kwargs = {} if self._model is None else {"model": self._model}
        questions = {
            "kind": Choice(
                instructions=f"Classify this {p['subject']} for {p['audience']}.",
                criteria=kind_criteria,
            ),
            "reason": Choice(
                instructions="What specifically changed.",
                criteria=reason_criteria,
            ),
        }
        for noul, instructions in p["nouls"].items():
            questions[noul] = Noul(instructions=instructions)
        result = self._client.system_one(
            state={"path": path, "old": old, "new": new},
            questions=questions,
            **kwargs,
        )
        kind_ans = result.choices["kind"]
        reason_ans = result.choices["reason"]
        nouls = {k: result.nouls[k].noul for k in p["nouls"]}
        return Verdict(kind_ans.choice, reason_ans.choice, kind_ans.confidence, nouls)


def _chat_system_prompt(profile: dict) -> str:
    p = profile
    kind_criteria = {k: v + " " + p["state_note"] for k, v in p["kinds"].items()}
    reason_criteria = {k: v["criteria"] for k, v in p["reasons"].items()}
    noul_lines = "\n".join(f"  {k}: {v}" for k, v in p["nouls"].items())
    return (
        f"You classify a single {p['subject']}. Respond with ONLY a JSON object "
        "with these keys:\n"
        f'- "kind": one of {list(kind_criteria)} ' + json.dumps(kind_criteria) + "\n"
        f'- "reason": one of {list(reason_criteria)} ' + json.dumps(reason_criteria) + "\n"
        '- "confidence": number 0-1\n'
        f"- {list(p['nouls'])}: each a number 0-1 answering the matching yes/no question:\n"
        f"{noul_lines}\n"
        "No arithmetic, no prose reasoning in the output. JSON only."
    )


class ChatJudge:
    """Backed by any OpenAI-compatible chat endpoint. Defaults to OpenRouter
    so an existing OPENROUTER_API_KEY works with no other config."""

    def __init__(self, profile: dict, model: str | None = None):
        import requests

        self._requests = requests
        self._profile = profile
        self._system_prompt = _chat_system_prompt(profile)
        self._base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self._api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self._api_key:
            raise RuntimeError("Set OPENROUTER_API_KEY for --judge chat")
        self._model = model or os.environ.get("SDIFF_CHAT_MODEL", "openai/gpt-5-nano")

    def judge(self, path: str, old, new) -> Verdict:
        resp = self._requests.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": json.dumps({"path": path, "old": old, "new": new}, default=str)},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        nouls = {k: float(data.get(k, 0.5)) for k in self._profile["nouls"]}
        kind = data.get("kind", KINDS[2])
        if kind not in KINDS:
            kind = KINDS[2]
        return Verdict(kind, data.get("reason", "other"), float(data.get("confidence", 0.5)), nouls)


JUDGES = {"jev": JevJudge, "chat": ChatJudge}


# ---------------------------------------------------------------------------
# classify + report
# ---------------------------------------------------------------------------

def match_rule(path: str, old, new, profile: dict) -> tuple[str, str] | None:
    """Deterministic prefilter: first profile rule whose glob matches `path`
    wins, no judge call needed. A static rule returns its fixed (kind,
    reason); a comparator rule runs the named comparator against (old, new)
    and returns its verdict, or None to fall through to the judge -- the
    comparator ran and had no opinion, it isn't "no rule matched"."""
    for rule in profile.get("rules", []):
        if not fnmatch.fnmatch(path, rule["glob"]):
            continue
        if "comparator" in rule:
            return COMPARATORS[rule["comparator"]](old, new)
        return rule["kind"], rule["reason"]
    return None


def classify_leaf(judge, profile: dict, path: str, old, new) -> dict:
    """Classify one changed leaf. Returns kind/flag/reason-code/confidence/
    nouls/source, not yet formatted with the path (see run(), which reuses
    this across leaves that share a cache key)."""
    rule = match_rule(path, old, new, profile)
    if rule is not None:
        kind, reason = rule
        return {"kind": kind, "flag": "", "reason_code": reason, "confidence": 1.0, "nouls": {}, "source": "rule"}
    if judge is None:
        return {"kind": "unknown", "flag": "?", "reason_code": "other", "confidence": 0.0, "nouls": {}, "source": "unreviewed"}
    v = judge.judge(path, old, new)
    kind, flag = apply_crosscheck(v)
    return {"kind": kind, "flag": flag, "reason_code": v.reason, "confidence": v.confidence, "nouls": v.nouls, "source": "judge"}


def run(old_obj, new_obj, profile: dict, judge=None, max_workers: int = 16) -> list[dict]:
    """judge=None means offline: only the profile's deterministic rules
    resolve anything, everything else comes back `unknown`/unreviewed."""
    align_keys = tuple(profile.get("align_keys", ("name",)))
    changes = diff(old_obj, new_obj, align_keys=align_keys)

    # Dedupe cache: identical (leaf name, old, new) triples -- e.g. the same
    # description reworded across 200 schemas -- classify once and reuse.
    # ponytail: cache key ignores full path context, so two leaves with the
    # same name/old/new but genuinely different meaning share a verdict;
    # widen the key (e.g. include parent path) if that ever misclassifies.
    def cache_key(path, old, new):
        leaf = path.rsplit(".", 1)[-1]
        return (leaf, repr(old), repr(new))

    unique: dict[tuple, tuple[str, object, object]] = {}
    for path, old, new in changes:
        unique.setdefault(cache_key(path, old, new), (path, old, new))

    raw_by_key: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(classify_leaf, judge, profile, path, old, new): key
            for key, (path, old, new) in unique.items()
        }
        for fut in as_completed(futs):
            raw_by_key[futs[fut]] = fut.result()

    results = []
    for path, old, new in changes:
        raw = raw_by_key[cache_key(path, old, new)]
        results.append({
            "path": path,
            "old": old,
            "new": new,
            "kind": raw["kind"],
            "flag": raw["flag"],
            "reason": format_reason(profile, raw["reason_code"], path, old, new),
            "confidence": raw["confidence"],
            "nouls": raw["nouls"],
            "source": raw["source"],
        })
    results.sort(key=lambda r: r["path"])
    return results


def _value_str(v) -> str:
    return "(absent)" if v is None else str(v)


def render(results: list[dict], json_output: bool) -> None:
    if json_output:
        print(json.dumps(results, indent=2, default=str))
        return

    breaking = [r for r in results if r["kind"] == KINDS[0]]
    behaviour = [r for r in results if r["kind"] == KINDS[1]]
    cosmetic = [r for r in results if r["kind"] == KINDS[2]]
    unknown = [r for r in results if r["kind"] not in KINDS]

    for r in breaking:
        label = f"BREAKING{r['flag']}"
        print(f"  {label:<10}{r['path']:<45}  {_value_str(r['old'])} -> {_value_str(r['new'])}")
        print(f'            "{r["reason"]}"')
    for r in behaviour:
        label = f"BEHAVIOUR{r['flag']}"
        print(f"  {label:<10}{r['path']:<45}  {_value_str(r['old'])} -> {_value_str(r['new'])}")
    if cosmetic:
        print(f"  COSMETIC  {len(cosmetic)} other changes")
    if unknown:
        print(f"  UNREVIEWED  {len(unknown)} changes not classified (no --judge, no rule matched)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff two structured files and classify each change.")
    p.add_argument("old", nargs="?")
    p.add_argument("new", nargs="?")
    p.add_argument("--profile", default=None,
                    help=f"built-in ({', '.join(BUILTIN_PROFILES)}) or path to a profile YAML; "
                         "auto-detected from the input if omitted")
    p.add_argument("--list-profiles", action="store_true", help="print built-in profile names and exit")
    p.add_argument("--judge", choices=list(JUDGES), default="jev")
    p.add_argument("--chat-model", default=None, help="override model for --judge chat")
    p.add_argument("--offline", action="store_true",
                    help="no judge, no network: only deterministic rules resolve; everything else is UNREVIEWED")
    p.add_argument("--workers", type=int, default=16, help="max concurrent judge calls")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.list_profiles:
        for name in BUILTIN_PROFILES:
            print(f"{name:<20}{_read_builtin_profile(name)['subject']}")
        return 0
    if args.old is None or args.new is None:
        p.error("the following arguments are required: old, new")

    try:
        old_obj, new_obj = load(args.old), load(args.new)
    except (OSError, yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        profile = load_profile(args.profile, old_obj, new_obj, args.old, args.new)
    except (OSError, yaml.YAMLError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.profile is None:
        print(f"# profile: {profile['name']} (auto-detected)", file=sys.stderr)

    try:
        judge = None
        if not args.offline:
            judge = JevJudge(profile) if args.judge == "jev" else ChatJudge(profile, model=args.chat_model)
        results = run(old_obj, new_obj, profile, judge, max_workers=args.workers)
    except Exception as e:  # noqa: BLE001 -- surface any judge/SDK failure plainly
        print(f"error: {e}", file=sys.stderr)
        return 2

    render(results, args.json)
    return 1 if any(r["kind"] == KINDS[0] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
