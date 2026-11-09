"""Тесты инструментов pull request: валидация и тела запросов к GitHub API."""

from __future__ import annotations

import pytest

from server import main


@pytest.fixture(autouse=True)
def _repo(monkeypatch):
    monkeypatch.setattr(main, "_repo_parts", lambda: ("owner", "repo"))
    monkeypatch.setattr(
        main, "_cfg", lambda name, default="": default if name != "GIT_TOKEN" else "token"
    )


def _capture(monkeypatch, response=None):
    calls: list[tuple] = []

    def fake(method, path, payload=None, accept="application/vnd.github+json"):
        calls.append((method, path, payload))
        return response if response is not None else {}

    monkeypatch.setattr(main, "_github", fake)
    return calls


def test_pr_create_builds_payload(monkeypatch):
    calls = _capture(monkeypatch, {"number": 7, "head": {"ref": "feature/x"}})

    main.pr_create("Заголовок", "feature/x", body="Описание")

    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/repos/owner/repo/pulls"
    assert payload == {
        "title": "Заголовок",
        "head": "feature/x",
        "base": "main",
        "draft": False,
        "body": "Описание",
    }


@pytest.mark.parametrize("branch", ["", "../x", "-x", "a..b", "x/", "x/.hidden"])
def test_pr_create_rejects_bad_branch(monkeypatch, branch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_create("Заголовок", branch)


def test_pr_create_requires_title(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_create("x", "feature/x")


def test_pr_update_rejects_empty_changes(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_update(1)


def test_pr_update_sends_only_given_fields(monkeypatch):
    calls = _capture(monkeypatch, {"number": 1, "body": "new"})

    main.pr_update(1, body="new", state="closed")

    assert calls[0][0] == "PATCH"
    assert calls[0][1] == "/repos/owner/repo/pulls/1"
    assert calls[0][2] == {"body": "new", "state": "closed"}


def test_pr_update_rejects_unknown_state(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_update(1, state="merged")


def test_pr_merge_payload(monkeypatch):
    calls = _capture(monkeypatch, {"merged": True, "sha": "abc", "message": "ok"})

    result = main.pr_merge(5, method="squash", commit_title="feat: x")

    assert calls[0][0] == "PUT"
    assert calls[0][2] == {"merge_method": "squash", "commit_title": "feat: x"}
    assert result["merged"] is True


def test_pr_merge_rejects_unknown_method(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_merge(5, method="fast-forward")


def test_pr_merge_deletes_head_branch(monkeypatch):
    calls: list[tuple] = []

    def fake(method, path, payload=None, accept="application/vnd.github+json"):
        calls.append((method, path, payload))
        if method == "GET":
            return {"head": {"ref": "feature/x"}}
        if method == "PUT":
            return {"merged": True, "sha": "abc", "message": "ok"}
        return {}

    monkeypatch.setattr(main, "_github", fake)

    result = main.pr_merge(5, delete_branch=True)

    delete = next(call for call in calls if call[0] == "DELETE")
    assert delete[1].endswith("/git/refs/heads/feature%2Fx")
    assert result["branch_deleted"] is True


def test_pr_review_rejects_unknown_event(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_review(1, event="LGTM", body="ok")


def test_pr_review_comment_event_needs_body(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_review(1, event="COMMENT")


def test_pr_review_builds_inline_comments(monkeypatch):
    calls = _capture(monkeypatch, {"id": 1, "state": "CHANGES_REQUESTED"})

    main.pr_review(
        1,
        event="request_changes",
        body="Нужны правки",
        comments=[{"path": "server/main.py", "line": 10, "body": "баг", "side": "left"}],
    )

    payload = calls[0][2]
    assert payload["event"] == "REQUEST_CHANGES"
    assert payload["comments"] == [
        {"path": "server/main.py", "line": 10, "side": "LEFT", "body": "баг"}
    ]


def test_pr_review_comment_needs_line(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_review(1, comments=[{"path": "a.py", "body": "баг"}])


def test_pr_review_comment_defaults_to_head_sha(monkeypatch):
    calls: list[tuple] = []

    def fake(method, path, payload=None, accept="application/vnd.github+json"):
        calls.append((method, path, payload))
        if method == "GET":
            return {"head": {"sha": "deadbeef"}}
        return {"id": 9, "path": "a.py", "line": 12}

    monkeypatch.setattr(main, "_github", fake)

    main.pr_review_comment(3, "замечание", "a.py", 12)

    post = next(call for call in calls if call[0] == "POST")
    assert post[2]["commit_id"] == "deadbeef"
    assert post[2]["line"] == 12
    assert post[2]["side"] == "RIGHT"


def test_pr_request_reviewers_payload(monkeypatch):
    calls = _capture(monkeypatch, {"requested_reviewers": [{"login": "octocat"}]})

    main.pr_request_reviewers(2, reviewers=["octocat"], teams=["core"])

    assert calls[0][1] == "/repos/owner/repo/pulls/2/requested_reviewers"
    assert calls[0][2] == {"reviewers": ["octocat"], "team_reviewers": ["core"]}


def test_pr_request_reviewers_validates_login(monkeypatch):
    _capture(monkeypatch)

    with pytest.raises(RuntimeError):
        main.pr_request_reviewers(2, reviewers=["bad/login"])


def test_pr_draft_uses_graphql(monkeypatch):
    monkeypatch.setattr(main, "_github", lambda *a, **k: {"node_id": "PR_1"})
    seen: dict = {}

    def fake_graphql(query, variables):
        seen.update(query=query, variables=variables)
        return {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}

    monkeypatch.setattr(main, "_graphql", fake_graphql)

    result = main.pr_draft(4, draft=False)

    assert "markPullRequestReadyForReview" in seen["query"]
    assert seen["variables"] == {"id": "PR_1"}
    assert result == {"draft": False, "node_id": "PR_1"}


def test_pr_labels_quotes_names(monkeypatch):
    calls: list[tuple] = []

    def fake(method, path, payload=None, accept="application/vnd.github+json"):
        calls.append((method, path, payload))
        if method == "GET":
            return [{"name": "security"}]
        return {}

    monkeypatch.setattr(main, "_github", fake)

    result = main.pr_labels(4, add=["security"], remove=["help wanted"])

    assert result == {"labels": ["security"]}
    assert any(
        call[1].endswith("/labels/help%20wanted") for call in calls if call[0] == "DELETE"
    )


def test_pr_checks_degrades_when_status_forbidden(monkeypatch):
    def fake(method, path, payload=None, accept="application/vnd.github+json"):
        if path.endswith("/pulls/1"):
            return {"head": {"sha": "abc"}}
        if path.endswith("/status"):
            raise RuntimeError("GitHub 403: forbidden")
        return {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}

    monkeypatch.setattr(main, "_github", fake)

    result = main.pr_checks(1)

    assert result["head_sha"] == "abc"
    assert result["check_runs"][0]["name"] == "ci"
    assert result["errors"] and "403" in result["errors"][0]
