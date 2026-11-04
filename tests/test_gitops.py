"""Тесты обёртки над git: адрес репозитория, пути, маскирование, локальный git."""

from __future__ import annotations

import base64

import pytest

from server import gitops


SCHEME = "https://"
GOOD = f"{SCHEME}github.com/GolderDmitry/runturu-git"


def test_normalize_repo_url():
    assert gitops.normalize_repo_url(GOOD) == GOOD
    assert gitops.normalize_repo_url(f" {GOOD}.git/ ") == GOOD


@pytest.mark.parametrize(
    "value",
    [
        "",
        "git@github.com:o/r.git",
        f"{SCHEME}github.com/o/r".replace("https", "http"),
        f"{SCHEME}git" + "lab.com/o/r",
        f"{SCHEME}github.com/o",
        f"{SCHEME}github.com/o/r/../../etc",
        f"{SCHEME}user:pass@github.com/o/r",
        f"{SCHEME}github.com.evil.tld/o/r",
    ],
)
def test_normalize_repo_url_rejects(value):
    with pytest.raises(gitops.GitError):
        gitops.normalize_repo_url(value)


def test_safe_path(monkeypatch, tmp_path):
    monkeypatch.setattr(gitops, "REPO_DIR", tmp_path)
    assert gitops.safe_path("docs/readme.md") == tmp_path / "docs/readme.md"
    assert gitops.safe_path("/etc/passwd").parts[-2:] == ("etc", "passwd")
    with pytest.raises(gitops.GitError):
        gitops.safe_path("../escape")
    with pytest.raises(gitops.GitError):
        gitops.safe_path("")


def test_mask_hides_token():
    fake = "ghp_" + "a1b2c3d4" * 5
    text = f"failed with {fake} and {base64.b64encode(f'x-access-token:{fake}'.encode()).decode()}"
    masked = gitops.mask(text, fake)
    assert fake not in masked
    assert "a1b2c3d4" not in masked
    assert masked.count("***") == 2


def test_env_sets_extraheader(monkeypatch, tmp_path):
    monkeypatch.setattr(gitops, "HOME_DIR", tmp_path / "home")
    fake = "t" * 40
    env = gitops._env(fake)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == f"http.{gitops.SCHEME}{gitops.GITHUB_HOST}/.extraheader"
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].split(" ")[-1]).decode()
    assert decoded == f"x-access-token:{fake}"
    assert fake not in str(env["GIT_CONFIG_KEY_0"])


def test_env_without_token(monkeypatch, tmp_path):
    monkeypatch.setattr(gitops, "HOME_DIR", tmp_path / "home")
    env = gitops._env("")
    assert "GIT_CONFIG_KEY_0" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_truncate():
    assert gitops.truncate("abc", 5) == "abc"
    cut = gitops.truncate("x" * 50, 10)
    assert cut.startswith("x" * 10)
    assert "обрезано" in cut


def test_local_git_roundtrip(monkeypatch, tmp_path):
    """Реальный git без сети: init → файл → commit → log → ветка."""
    repo = tmp_path / "repo"
    monkeypatch.setattr(gitops, "REPO_DIR", repo)
    monkeypatch.setattr(gitops, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(gitops, "HOME_DIR", tmp_path / "home")

    gitops.git(["init", "-q", "-b", "main", str(repo)], cwd=tmp_path)
    gitops.git(["config", "user.name", "Test"], token="")
    gitops.git(["config", "user.email", "test@example.com"], token="")

    target = gitops.safe_path("hello.txt")
    target.write_text("привет\n", encoding="utf-8")

    gitops.git(["add", "-A"], token="")
    gitops.git(["commit", "-m", "first", "-q"], token="")

    assert gitops.current_branch() == "main"
    assert gitops.has_commits()
    log = gitops.git(["log", "--pretty=format:%s"], token="")
    assert log.strip() == "first"

    gitops.git(["checkout", "-q", "-b", "feature"], token="")
    assert gitops.current_branch() == "feature"


def test_git_refuses_flags():
    with pytest.raises(gitops.GitError):
        gitops.git(["--version"])
