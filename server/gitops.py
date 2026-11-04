"""Обёртка над git CLI для CMCPT «Github».

Один репозиторий github.com на подключение. Токен передаётся git через
`GIT_CONFIG_*` (extraheader) — он не попадает ни в argv, ни в `.git/config`,
а в выводе инструментов маскируется.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

GITHUB_HOST = "github.com"
SCHEME = "https://"
ROOT_DIR = Path(os.environ.get("GIT_WORKDIR", "/tmp/cmcp-github"))
REPO_DIR = ROOT_DIR / "repo"
HOME_DIR = ROOT_DIR / "home"

GIT_TIMEOUT = int(os.environ.get("GIT_TIMEOUT", "180"))
MAX_OUTPUT = 40_000
MAX_FILE_BYTES = 400_000

REPO_URL_RE = re.compile(
    r"^"
    + re.escape(SCHEME + GITHUB_HOST)
    + r"/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})$"
)
DEFAULT_NAME = "Runturu Agent"
DEFAULT_EMAIL = "agent@runturu.com"


class GitError(RuntimeError):
    """Ошибка git с уже очищенным от секретов текстом."""


def mask(text: str, token: str) -> str:
    """Убирает токен и его base64-представление из любого текста."""
    if not text or not token:
        return text or ""
    creds = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return text.replace(creds, "***").replace(token, "***")


def normalize_repo_url(url: str) -> str:
    """Проверяет адрес репозитория: только GitHub по HTTPS."""
    hint = f"{SCHEME}{GITHUB_HOST}/<owner>/<repo>"
    value = (url or "").strip()
    if not value:
        raise GitError(f"Не задан адрес репозитория: укажите {hint}")
    if value.startswith("git@"):
        raise GitError(f"SSH-адреса не поддерживаются: используйте {hint}")
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4].rstrip("/")
    if not REPO_URL_RE.match(value):
        raise GitError(f"Ожидается {hint} — только GitHub по HTTPS")
    return value


def safe_path(relative: str) -> Path:
    """Путь внутри рабочей копии; выход за её пределы запрещён."""
    rel = (relative or "").strip().lstrip("/")
    if not rel:
        raise GitError("Путь не задан")
    if ".." in Path(rel).parts:
        raise GitError("Путь не может выходить за пределы репозитория")
    root = REPO_DIR.resolve()
    target = (REPO_DIR / rel).resolve()
    if target != root and root not in target.parents:
        raise GitError("Путь не может выходить за пределы репозитория")
    return target


def _env(token: str) -> dict[str, str]:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(HOME_DIR),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if token:
        creds = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{GITHUB_HOST}/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {creds}"
    return env


def git(
    args: list[str],
    token: str = "",
    cwd: Path | None = None,
    timeout: int = GIT_TIMEOUT,
    check: bool = True,
) -> str:
    """Запускает `git <args>` без shell и возвращает stdout."""
    code, stdout, stderr = _exec(args, token=token, cwd=cwd, timeout=timeout)
    if code != 0 and check:
        detail = (stderr or stdout or f"git {args[0]} завершился с кодом {code}").strip()
        raise GitError(detail[:4000])
    return stdout


def git_combined(
    args: list[str], token: str = "", timeout: int = GIT_TIMEOUT, check: bool = False
) -> str:
    """Как `git()`, но объединяет stdout и stderr (push/pull пишут в stderr)."""
    code, stdout, stderr = _exec(args, token=token, timeout=timeout)
    text = (stdout + stderr).strip()
    if code != 0 and check:
        raise GitError(text[:4000] or f"git {args[0]} завершился с кодом {code}")
    return text


def _exec(
    args: list[str],
    token: str = "",
    cwd: Path | None = None,
    timeout: int = GIT_TIMEOUT,
) -> tuple[int, str, str]:
    if not args:
        raise GitError("Не переданы аргументы git")
    if args[0].startswith("-"):
        raise GitError("Первым аргументом должна быть команда git, а не флаг")
    workdir = cwd or (REPO_DIR if REPO_DIR.exists() else None)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workdir) if workdir else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_env(token),
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git не установлен в контейнере") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]}: превышен таймаут {timeout} с") from exc
    return proc.returncode, mask(proc.stdout or "", token), mask(proc.stderr or "", token)


def current_branch(token: str = "") -> str:
    try:
        value = git(["rev-parse", "--abbrev-ref", "HEAD"], token=token).strip()
    except GitError:
        return ""
    return "" if value in ("HEAD", "") else value


def has_commits(token: str = "") -> bool:
    try:
        git(["rev-parse", "--verify", "HEAD"], token=token)
        return True
    except GitError:
        return False


def remote_url(token: str = "") -> str:
    try:
        return git(["remote", "get-url", "origin"], token=token).strip()
    except GitError:
        return ""


def _configure(repo_url: str, token: str, name: str, email: str) -> None:
    git(["remote", "add", "origin", repo_url], token=token, check=False)
    git(["remote", "set-url", "origin", repo_url], token=token, check=False)
    git(["config", "user.name", name or DEFAULT_NAME], token=token)
    git(["config", "user.email", email or DEFAULT_EMAIL], token=token)
    git(["config", "commit.gpgsign", "false"], token=token)
    git(["config", "core.quotepath", "false"], token=token)
    git(["config", "advice.detachedHead", "false"], token=token)


def _initial_checkout(branch: str, token: str) -> None:
    ref = f"origin/{branch}"
    try:
        git(["rev-parse", "--verify", f"refs/remotes/{ref}"], token=token)
        git(["checkout", "-B", branch, ref], token=token)
    except GitError:
        git(["checkout", "--orphan", branch], token=token, check=False)
        git(["reset", "--hard"], token=token, check=False)


def fetch(token: str = "", timeout: int = GIT_TIMEOUT) -> str:
    return git(["fetch", "--prune", "--tags", "origin"], token=token, timeout=timeout)


def ensure_repo(
    repo_url: str,
    branch: str = "",
    token: str = "",
    name: str = DEFAULT_NAME,
    email: str = DEFAULT_EMAIL,
    force: bool = False,
) -> str:
    """Готовит рабочую копию и возвращает нормализованный адрес.

    Существующую копию не переключает: текущая ветка — состояние агента,
    fetch делает только `repo_sync` или первичное клонирование.
    """
    normalized = normalize_repo_url(repo_url)
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    branch = (branch or "main").strip() or "main"

    exists = (REPO_DIR / ".git").exists()
    if exists and force:
        shutil.rmtree(REPO_DIR, ignore_errors=True)
        exists = False
    if exists and remote_url(token) != normalized:
        shutil.rmtree(REPO_DIR, ignore_errors=True)
        exists = False
    if exists:
        _configure(normalized, token, name, email)
        return normalized

    shutil.rmtree(REPO_DIR, ignore_errors=True)
    REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    git(["init", "-q", "-b", branch, str(REPO_DIR)], token=token, cwd=REPO_DIR.parent)
    _configure(normalized, token, name, email)
    fetch(token)
    _initial_checkout(branch, token)
    return normalized


def repo_size() -> str:
    """Размер рабочей копии вместе с `.git` в человекочитаемом виде."""
    total = 0
    for path in ROOT_DIR.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    if total < 1024:
        return f"{total} Б"
    value = total / 1024
    for unit in ("КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… обрезано ({len(text)} символов, показано {limit})"
