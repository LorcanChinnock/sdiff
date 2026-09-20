"""One pytest file, zero network. FakeJudge stands in for Jev/chat."""
from sdiff import Verdict, apply_crosscheck, diff, flatten, render, run


class FakeJudge:
    def __init__(self, answers: dict[str, Verdict]):
        self.answers = answers

    def judge(self, path, old, new):
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
    results = run(old, new, judge)
    assert len(results) == 1 and results[0]["kind"] == "breaking"

    render(results, json_output=False)
    out = capsys.readouterr().out
    assert "BREAKING" in out and "5x" in out  # 20 -> 100 ratio computed in code

    no_breaking = [{**results[0], "kind": "cosmetic"}]
    render(no_breaking, json_output=False)
    assert "COSMETIC  1 other changes" in capsys.readouterr().out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
