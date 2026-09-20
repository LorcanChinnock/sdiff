"""One pytest file, zero network. FakeJudge stands in for Jev/chat."""
import pytest

from sdiff import (
    BUILTIN_PROFILES,
    Verdict,
    _detect_profile_name,
    _read_builtin_profile,
    _semver_compare,
    apply_crosscheck,
    diff,
    flatten,
    load,
    load_profile,
    match_rule,
    render,
    run,
    validate_profile,
)

OPENAPI_PROFILE = load_profile("openapi")


class FakeJudge:
    def __init__(self, answers: dict[str, Verdict]):
        self.answers = answers
        self.calls = 0

    def judge(self, path, old, new):
        self.calls += 1
        return self.answers[path]


def test_flatten_aligns_list_of_dicts_by_name_and_collapses_scalar_lists():
    spec = {
        "paths": {
            "/users": {
                "get": {
                    "parameters": [
                        {"name": "limit", "default": 20},
                        {"name": "offset", "default": 0},
                    ]
                }
            }
        },
        "required": ["b", "a"],
    }
    leaves = flatten(spec)
    assert leaves["paths./users.get.parameters.limit.default"] == 20
    assert leaves["paths./users.get.parameters.offset.default"] == 0
    assert leaves["required"] == ["a", "b"]  # sorted whole-list leaf, no index churn


def test_flatten_aligns_by_id_when_align_keys_says_so():
    spec = {"items": [{"id": "x", "v": 1}, {"id": "y", "v": 2}]}
    leaves = flatten(spec, align_keys=("id",))
    assert leaves["items.x.v"] == 1
    assert leaves["items.y.v"] == 2


def test_diff_reports_added_and_removed_as_none_on_absent_side():
    a = {"x": {"name": "keep", "y": 1}}
    b = {"x": {"name": "keep", "y": 1}, "z": 2}
    changes = dict((p, (o, n)) for p, o, n in diff(a, b))
    assert changes["z"] == (None, 2)


def test_crosscheck_escalates_cosmetic_to_behaviour_on_high_client_impact():
    v = Verdict(kind="cosmetic", reason="other", confidence=0.9,
                nouls={"client_code_must_change": 0.95, "old_requests_still_valid": 0.1, "doc_or_example_only": 0.0})
    kind, flag = apply_crosscheck(v)
    assert kind == "behaviour" and flag == "!"


def test_crosscheck_never_downgrades_breaking():
    v = Verdict(kind="breaking", reason="removed", confidence=0.9,
                nouls={"client_code_must_change": 0.0, "old_requests_still_valid": 1.0, "doc_or_example_only": 0.0})
    kind, _ = apply_crosscheck(v)
    assert kind == "breaking"


def test_run_and_exit_code_driven_by_breaking(capsys):
    old = {"paths": {"/users": {"get": {"parameters": [{"name": "limit", "default": 20}]}}}}
    new = {"paths": {"/users": {"get": {"parameters": [{"name": "limit", "default": 100}]}}}}
    path = "paths./users.get.parameters.limit.default"
    judge = FakeJudge({
        path: Verdict(kind="breaking", reason="default_changed", confidence=0.9,
                      nouls={"client_code_must_change": 1.0, "old_requests_still_valid": 0.0, "doc_or_example_only": 0.0})
    })
    results = run(old, new, OPENAPI_PROFILE, judge)
    assert len(results) == 1 and results[0]["kind"] == "breaking"

    render(results, json_output=False)
    out = capsys.readouterr().out
    assert "BREAKING" in out and "5x" in out  # 20 -> 100 ratio computed in code

    no_breaking = [{**results[0], "kind": "cosmetic"}]
    render(no_breaking, json_output=False)
    assert "COSMETIC  1 other changes" in capsys.readouterr().out


def test_rule_prefilter_skips_the_judge():
    old = {"paths": {"/x": {"get": {"description": "old"}}}}
    new = {"paths": {"/x": {"get": {"description": "new"}}}}
    judge = FakeJudge({})  # would KeyError if ever called
    results = run(old, new, OPENAPI_PROFILE, judge)
    assert len(results) == 1
    assert results[0]["kind"] == "cosmetic"
    assert results[0]["source"] == "rule"
    assert judge.calls == 0


def test_dedupe_cache_collapses_identical_leaf_changes_to_one_judge_call():
    old = {"schemas": {"A": {"note": "x"}, "B": {"note": "x"}, "C": {"note": "x"}}}
    new = {"schemas": {"A": {"note": "y"}, "B": {"note": "y"}, "C": {"note": "y"}}}
    v = Verdict(kind="cosmetic", reason="other", confidence=0.9,
                nouls={"client_code_must_change": 0.0, "old_requests_still_valid": 1.0, "doc_or_example_only": 1.0})
    judge = FakeJudge({"schemas.A.note": v, "schemas.B.note": v, "schemas.C.note": v})
    results = run(old, new, OPENAPI_PROFILE, judge)
    assert len(results) == 3
    assert judge.calls == 1  # same leaf name + old + new -> classified once


def test_offline_mode_reports_unreviewed_and_never_calls_judge():
    old = {"paths": {"/x": {"get": {"operationId": "foo"}}}}
    new = {"paths": {"/x": {"get": {"operationId": "bar"}}}}
    results = run(old, new, OPENAPI_PROFILE, judge=None)
    assert len(results) == 1
    assert results[0]["kind"] == "unknown"
    assert results[0]["flag"] == "?"
    assert results[0]["source"] == "unreviewed"


def test_load_profile_detects_openapi_from_top_level_key():
    profile = load_profile(None, old_obj={"openapi": "3.0.0"}, new_obj={"openapi": "3.0.0"})
    assert profile["name"] == "openapi"


def test_load_profile_falls_back_to_generic():
    profile = load_profile(None, old_obj={"foo": "bar"}, new_obj={"foo": "baz"})
    assert profile["name"] == "generic"


def test_validate_profile_rejects_missing_kinds():
    bad = {
        "name": "bad", "subject": "x", "audience": "y", "align_keys": ["name"],
        "state_note": "n", "kinds": {"breaking": "b"}, "reasons": {}, "nouls": {},
    }
    with pytest.raises(ValueError):
        validate_profile(bad, "bad")


def test_match_rule_first_match_wins():
    profile = {"rules": [{"glob": "*.description", "kind": "cosmetic", "reason": "doc_only"}]}
    assert match_rule("foo.description", "a", "b", profile) == ("cosmetic", "doc_only")
    assert match_rule("foo.other", "a", "b", profile) is None


def test_load_reads_toml(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text('[server]\nport = 8080\n', encoding="utf-8")
    assert load(str(p)) == {"server": {"port": 8080}}


# --- catalogue: conformance, detection precedence, comparators ------------

@pytest.mark.parametrize("name", BUILTIN_PROFILES)
def test_builtin_profile_conforms(name):
    """Rot guard: every shipped profile must be internally consistent. Any
    future profile has to pass this too."""
    profile = _read_builtin_profile(name)
    validate_profile(profile, name)  # raises on any structural problem
    for k, v in profile["reasons"].items():
        assert "criteria" in v and "template" in v, k
    for rule in profile.get("rules", []):
        if "kind" in rule:
            assert rule["kind"] == "cosmetic", (
                f"{name}: static rule {rule['glob']!r} asserts {rule['kind']!r}; "
                "a static glob may only assert cosmetic, use a comparator for anything else"
            )
    if name != "generic":
        detect = profile.get("detect", {})
        assert detect.get("keys") or detect.get("files"), f"{name}: no detect.keys or detect.files"


def test_detection_prefers_content_over_filename():
    # a real k8s Deployment, filename that screams "config"
    k8s_doc = {"apiVersion": "apps/v1", "kind": "Deployment"}
    assert _detect_profile_name(k8s_doc, k8s_doc, "app-config.yaml", "app-config.yaml") == "k8s"

    # a real dependency manifest, filename that screams "policy"
    dep_doc = {"dependencies": {"foo": "1.0.0"}}
    assert _detect_profile_name(dep_doc, dep_doc, "policy-lockfile.json", "policy-lockfile.json") == "dependency-manifest"


def test_detection_falls_back_to_filename_when_no_content_match():
    assert _detect_profile_name({}, {}, "values.yaml", "values.yaml") == "k8s"
    assert _detect_profile_name({}, {}, "config.toml", "config.toml") == "config"


def test_detection_filename_matches_full_cli_path_not_just_bare_name():
    # a real invocation always passes a path, not a bare filename -- a
    # literal pattern like "values.yaml" must still match the basename.
    assert _detect_profile_name({}, {}, "/tmp/some/dir/values.yaml", "/tmp/some/dir/values.yaml") == "k8s"
    assert _detect_profile_name({}, {}, "C:\\repo\\config.yaml", "C:\\repo\\config.yaml") == "config"


@pytest.mark.parametrize("old,new,expected", [
    ("1.2.3", "2.0.0", ("breaking", "type_changed")),
    ("1.2.3", "1.3.0", ("behaviour", "constraint_loosened")),
    ("1.2.3", "1.2.4", ("cosmetic", "doc_only")),
    ("^1.2.3", "^2.0.0", ("breaking", "type_changed")),
    ("workspace:*", "1.0.0", None),
])
def test_semver_comparator(old, new, expected):
    assert _semver_compare(old, new) == expected


def test_comparator_rule_end_to_end_no_judge_call_on_major_bump():
    profile = _read_builtin_profile("dependency-manifest")
    old = {"dependencies": {"left-pad": "1.2.3"}}
    new = {"dependencies": {"left-pad": "2.0.0"}}
    judge = FakeJudge({})  # would KeyError if ever called
    results = run(old, new, profile, judge)
    assert len(results) == 1
    assert results[0]["kind"] == "breaking"
    assert results[0]["source"] == "rule"
    assert judge.calls == 0


def test_validate_profile_rejects_rule_with_both_kind_and_comparator():
    bad = dict(OPENAPI_PROFILE)
    bad["rules"] = [{"glob": "*", "kind": "cosmetic", "reason": "doc_only", "comparator": "semver"}]
    with pytest.raises(ValueError):
        validate_profile(bad, "bad")


def test_validate_profile_rejects_non_cosmetic_static_rule():
    bad = dict(OPENAPI_PROFILE)
    bad["rules"] = [{"glob": "*", "kind": "breaking", "reason": "other"}]
    with pytest.raises(ValueError):
        validate_profile(bad, "bad")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
