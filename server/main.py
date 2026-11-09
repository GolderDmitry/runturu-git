"""CMCPT «Github»: полный доступ к одному git-репозиторию GitHub.

Транспорт — streamable HTTP, MCP endpoint `/mcp`, health `/health`.
Настройки подключения приходят от гейтвея Runturu в заголовке
`X-Runturu-Config` (base64url JSON) и читаются на каждый запрос.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from server import gitops

SERVER_NAME = "runturu-github"
MAX_LIST = 2000

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Полный доступ к одному git-репозиторию GitHub: рабочая копия, чтение "
        "файлов и истории, коммиты, ветки, merge/rebase, push (в том числе "
        "force), произвольные git-команды и работа с pull request: создание, "
        "изменение, слияние, ревью с замечаниями к строкам, CI и метки. "
        "Репозиторий и токен задаются в настройках подключения."
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_CFG: ContextVar[dict[str, str]] = ContextVar("cmcp_config", default={})


def _parse_config(scope: Any) -> dict[str, str]:
    """Параметры инстанса CMCP из X-Runturu-Config (base64url JSON)."""
    headers = Headers(scope=scope)
    raw = (headers.get("x-runturu-config", "") or "").strip()
    data: dict[str, str] = {}
    if raw:
        try:
            padded = raw + "=" * (-len(raw) % 4)
            parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if value is not None:
                        data[str(key)] = str(value)
        except Exception:  # noqa: BLE001
            pass
    return data


class ConfigMiddleware:
    """Кладёт параметры CMCP в contextvar на время HTTP-запроса."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _CFG.set(_parse_config(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _CFG.reset(token)


# ─────────────────────────── Настройки ───────────────────────────


def _cfg(name: str, default: str = "") -> str:
    value = str(_CFG.get().get(name) or "").strip()
    if value:
        return value
    return os.environ.get(name, "").strip() or default


def _workspace(force: bool = False) -> tuple[str, str, str]:
    """Возвращает (адрес репозитория, токен, ветка по умолчанию)."""
    url = _cfg("REPO_URL")
    token = _cfg("GIT_TOKEN")
    branch = _cfg("DEFAULT_BRANCH", "main")
    name = _cfg("GIT_USER_NAME", gitops.DEFAULT_NAME)
    email = _cfg("GIT_USER_EMAIL", gitops.DEFAULT_EMAIL)
    try:
        normalized = gitops.ensure_repo(url, branch, token, name, email, force=force)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    return normalized, token, branch or "main"


def _run(args: list[str], token: str = "", **kwargs) -> str:
    try:
        return gitops.git(args, token=token, **kwargs)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc


def _lines(text: str, limit: int = MAX_LIST) -> list[str]:
    items = [line for line in (text or "").splitlines() if line.strip()]
    return items[:limit]


# ─────────────────────────── Чтение ───────────────────────────


@mcp.tool()
def repo_info() -> dict:
    """Состояние рабочей копии: ветка, HEAD, изменения, размер, remote."""
    url, token, branch = _workspace()
    head = _run(["rev-parse", "--short", "HEAD"], token=token, check=False).strip()
    status = _run(["status", "--porcelain=v1", "-b"], token=token, check=False)
    rows = _lines(status)
    summary = rows[0] if rows and rows[0].startswith("##") else ""
    changes = [row for row in rows if not row.startswith("##")]
    return {
        "repo": url,
        "branch": gitops.current_branch(token) or "(нет коммитов)",
        "default_branch": branch,
        "head": head or "(нет коммитов)",
        "ahead_behind": summary,
        "dirty": bool(changes),
        "changes_count": len(changes),
        "changes": changes[:200],
        "size": gitops.repo_size(),
        "workspace": str(gitops.REPO_DIR),
    }


@mcp.tool()
def list_files(path: str = "", ref: str = "", limit: int = 500) -> dict:
    """Список файлов: в рабочей копии или в произвольной ревизии (ref)."""
    url, token, _branch = _workspace()
    limit = max(1, min(int(limit or 500), MAX_LIST))
    prefix = (path or "").strip().strip("/")
    if ref:
        args = ["ls-tree", "-r", "--name-only", ref]
        if prefix:
            args += ["--", prefix]
        files = _lines(_run(args, token=token), limit + 1)
    else:
        files = _lines(
            _run(["ls-files", "-co", "--exclude-standard"], token=token), MAX_LIST * 5
        )
        if prefix:
            files = [item for item in files if item == prefix or item.startswith(prefix + "/")]
        files = files[: limit + 1]
    return {
        "repo": url,
        "source": ref or "working tree",
        "count": len(files),
        "truncated": len(files) > limit,
        "files": files[:limit],
    }


@mcp.tool()
def read_file(path: str, ref: str = "") -> dict:
    """Прочитать файл из рабочей копии или из ревизии (ref)."""
    url, token, _branch = _workspace()
    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise ValueError("Путь не задан")
    if ref:
        try:
            content = _run(["show", f"{ref}:{rel}"], token=token)
        except ValueError as exc:
            raise ValueError(f"{rel}@{ref}: {exc}") from exc
        size = len(content.encode("utf-8"))
        return {
            "path": rel,
            "ref": ref,
            "bytes": size,
            "truncated": size > gitops.MAX_FILE_BYTES,
            "content": content[: gitops.MAX_FILE_BYTES],
        }
    try:
        target = gitops.safe_path(rel)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    if not target.is_file():
        raise ValueError(f"Файл не найден: {rel}")
    raw = target.read_bytes()
    if b"\x00" in raw[:8000]:
        return {"path": rel, "binary": True, "bytes": len(raw)}
    text = raw.decode("utf-8", "replace")
    return {
        "path": rel,
        "ref": "",
        "bytes": len(raw),
        "truncated": len(raw) > gitops.MAX_FILE_BYTES,
        "content": text[: gitops.MAX_FILE_BYTES],
    }


@mcp.tool()
def git_log(limit: int = 20, ref: str = "", path: str = "") -> dict:
    """История коммитов: ref — ветка/тег, path — только по файлу."""
    url, token, _branch = _workspace()
    limit = max(1, min(int(limit or 20), 500))
    args = ["log", f"-{limit}", "--date=iso", "--pretty=format:%h|%an|%ad|%d|%s", ref or "HEAD"]
    if path:
        args += ["--", path.strip().lstrip("/")]
    text = _run(args, token=token, check=False).strip()
    commits = []
    for line in _lines(text, limit):
        parts = line.split("|", 4)
        if len(parts) == 5:
            commits.append(
                {
                    "sha": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "refs": parts[3].strip(),
                    "subject": parts[4],
                }
            )
    return {"repo": url, "ref": ref or "HEAD", "count": len(commits), "commits": commits}


@mcp.tool()
def git_diff(from_ref: str = "HEAD", to_ref: str = "", path: str = "", staged: bool = False) -> dict:
    """Разница: рабочая копия ↔ HEAD, между ревизиями или только staged."""
    url, token, _branch = _workspace()
    args = ["diff", "--no-color"]
    if staged:
        args.append("--staged")
        if from_ref and from_ref != "HEAD":
            args.append(from_ref)
    elif to_ref:
        args += [from_ref or "HEAD", to_ref]
    else:
        args.append(from_ref or "HEAD")
    if path:
        args += ["--", path.strip().lstrip("/")]
    text = _run(args, token=token, check=False)
    return {
        "repo": url,
        "from": from_ref or "HEAD",
        "to": to_ref or ("staged" if staged else "working tree"),
        "empty": not text.strip(),
        "diff": gitops.truncate(text),
    }


@mcp.tool()
def git_show(ref: str = "HEAD", path: str = "") -> dict:
    """Показать коммит целиком или файл в ревизии."""
    url, token, _branch = _workspace()
    target = ref or "HEAD"
    if path:
        target = f"{target}:{path.strip().lstrip('/')}"
    text = _run(["show", "--no-color", "--stat", "--date=iso", target], token=token, check=False)
    return {"repo": url, "ref": target, "content": gitops.truncate(text)}


@mcp.tool()
def git_blame(path: str, ref: str = "") -> dict:
    """Авторство строк файла."""
    url, token, _branch = _workspace()
    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise ValueError("Путь не задан")
    text = _run(["blame", "--date=short", ref or "HEAD", "--", rel], token=token)
    return {"repo": url, "path": rel, "blame": gitops.truncate(text)}


@mcp.tool()
def list_branches(remote: bool = True) -> dict:
    """Локальные и удалённые ветки."""
    url, token, _branch = _workspace()
    if remote:
        text = _run(["branch", "-a", "--no-color", "-vv"], token=token, check=False)
    else:
        text = _run(["branch", "--no-color", "-vv"], token=token)
    current = ""
    for line in text.splitlines():
        if line.startswith("*"):
            current = line[1:].split()[0]
    return {"repo": url, "current": current, "branches": gitops.truncate(text)}


@mcp.tool()
def list_tags() -> dict:
    """Теги репозитория."""
    url, token, _branch = _workspace()
    text = _run(["tag", "--list", "--sort=-creatordate"], token=token, check=False)
    return {"repo": url, "tags": _lines(text, 500)}


# ─────────────────────────── Запись ───────────────────────────


@mcp.tool()
def write_file(path: str, content: str, create_dirs: bool = True) -> dict:
    """Создать или перезаписать файл в рабочей копии (без коммита)."""
    url, token, _branch = _workspace()
    try:
        target = gitops.safe_path(path)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    if target.is_dir():
        raise ValueError(f"Это каталог: {path}")
    if not target.parent.exists():
        if not create_dirs:
            raise ValueError(f"Каталог не существует: {target.parent.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
    data = (content or "").encode("utf-8")
    target.write_bytes(data)
    return {"repo": url, "path": path.strip().lstrip("/"), "bytes": len(data), "saved": True}


@mcp.tool()
def delete_file(path: str) -> dict:
    """Удалить файл из рабочей копии (без коммита)."""
    url, token, _branch = _workspace()
    try:
        target = gitops.safe_path(path)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    if target.is_dir():
        raise ValueError(f"Это каталог, удаление каталогов не поддерживается: {path}")
    if not target.exists():
        raise ValueError(f"Файл не найден: {path}")
    target.unlink()
    return {"repo": url, "path": path.strip().lstrip("/"), "deleted": True}


@mcp.tool()
def commit(message: str, paths: list[str] | None = None, all: bool = True, amend: bool = False) -> dict:
    """Закоммитить изменения: конкретные пути или все (all=true)."""
    url, token, _branch = _workspace()
    text = (message or "").strip()
    if not text and not amend:
        raise ValueError("Нужно сообщение коммита")
    if paths:
        _run(["add", "--", *[item.strip().lstrip("/") for item in paths if item.strip()]], token=token)
    elif all:
        _run(["add", "-A"], token=token)
    args = ["commit", "-m", text] if text else ["commit", "--amend", "--no-edit"]
    if amend:
        args.append("--amend")
    _run(args, token=token)
    head = _run(["log", "-1", "--pretty=format:%h|%an|%ad|%s", "--date=iso"], token=token).strip()
    return {"repo": url, "commit": head, "amended": bool(amend)}


@mcp.tool()
def push(branch: str = "", force: bool = False, tags: bool = False, dry_run: bool = False) -> dict:
    """Отправить ветку в origin. force — `--force-with-lease`."""
    url, token, _branch = _workspace()
    if tags:
        args = ["push", "origin", "--tags"]
    else:
        target = (branch or gitops.current_branch(token)).strip()
        if not target:
            raise ValueError("Не удалось определить ветку: укажите branch")
        args = ["push", "origin", f"HEAD:{target}"]
    if force and not tags:
        args.insert(1, "--force-with-lease")
    if dry_run:
        args.insert(1, "--dry-run")
    try:
        text = gitops.git_combined(args, token=token, check=True)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "repo": url,
        "force": bool(force),
        "dry_run": bool(dry_run),
        "output": gitops.truncate(text or "ok"),
    }


@mcp.tool()
def repo_sync(rebase: bool = False, prune: bool = True) -> dict:
    """Забрать изменения из origin (fetch + pull)."""
    url, token, branch = _workspace()
    before = _run(["rev-parse", "HEAD"], token=token, check=False).strip()
    fetch_args = ["fetch", "--tags", "origin"]
    if prune:
        fetch_args.insert(1, "--prune")
    try:
        gitops.git_combined(fetch_args, token=token, check=True)
    except gitops.GitError as exc:
        raise ValueError(str(exc)) from exc
    current = gitops.current_branch(token) or branch
    pull_args = ["pull", "--rebase" if rebase else "--ff-only", "origin", current]
    text = gitops.git_combined(pull_args, token=token)
    after = _run(["rev-parse", "HEAD"], token=token, check=False).strip()
    return {
        "repo": url,
        "branch": current,
        "rebase": bool(rebase),
        "changed": before != after,
        "before": before[:12],
        "after": after[:12],
        "output": gitops.truncate(text.strip()),
    }


@mcp.tool()
def create_branch(name: str, from_ref: str = "", checkout: bool = True) -> dict:
    """Создать ветку (по умолчанию от текущего HEAD)."""
    url, token, _branch = _workspace()
    target = (name or "").strip()
    if not target:
        raise ValueError("Не задано имя ветки")
    if checkout:
        args = ["checkout", "-B", target]
        if from_ref:
            args.append(from_ref)
    else:
        args = ["branch", target]
        if from_ref:
            args.append(from_ref)
    _run(args, token=token)
    return {"repo": url, "branch": target, "checked_out": bool(checkout), "from": from_ref or "HEAD"}


@mcp.tool()
def checkout(ref: str, create: bool = False) -> dict:
    """Переключиться на ветку/тег/коммит; create=true — создать ветку."""
    url, token, _branch = _workspace()
    target = (ref or "").strip()
    if not target:
        raise ValueError("Не задана ревизия")
    args = ["checkout", "-B", target] if create else ["checkout", target]
    _run(args, token=token)
    return {"repo": url, "ref": target, "branch": gitops.current_branch(token)}


@mcp.tool()
def delete_branch(name: str, remote: bool = False, force: bool = True) -> dict:
    """Удалить ветку локально и/или в origin."""
    url, token, _branch = _workspace()
    target = (name or "").strip()
    if not target:
        raise ValueError("Не задано имя ветки")
    current = gitops.current_branch(token)
    if target == current:
        raise ValueError("Нельзя удалить ветку, на которой сейчас рабочая копия")
    result = {}
    if remote:
        result["remote"] = _run(["push", "origin", "--delete", target], token=token).strip()
    result["local"] = _run(["branch", "-D" if force else "-d", target], token=token).strip()
    return {"repo": url, "branch": target, "result": result}


@mcp.tool()
def reset(ref: str = "HEAD", mode: str = "hard") -> dict:
    """Переместить ветку на ref: mode = soft | mixed | hard."""
    url, token, _branch = _workspace()
    value = (mode or "hard").strip().lower()
    if value not in ("soft", "mixed", "hard"):
        raise ValueError("mode должен быть soft, mixed или hard")
    _run(["reset", f"--{value}", ref or "HEAD"], token=token)
    head = _run(["rev-parse", "--short", "HEAD"], token=token, check=False).strip()
    return {"repo": url, "mode": value, "ref": ref, "head": head}


@mcp.tool()
def merge(ref: str, no_ff: bool = False, message: str = "", ff_only: bool = False) -> dict:
    """Слить ref в текущую ветку."""
    url, token, _branch = _workspace()
    args = ["merge", "--no-edit"]
    if no_ff:
        args.append("--no-ff")
    if ff_only:
        args.append("--ff-only")
    if message.strip():
        args += ["-m", message.strip()]
    args.append(ref.strip())
    text = _run(args, token=token)
    return {"repo": url, "ref": ref, "output": gitops.truncate(text.strip())}


@mcp.tool()
def rebase(onto: str = "", action: str = "start") -> dict:
    """Rebase текущей ветки на onto; action = start | continue | abort | skip."""
    url, token, _branch = _workspace()
    step = (action or "start").strip().lower()
    if step == "start":
        if not (onto or "").strip():
            raise ValueError("Не задано onto")
        args = ["rebase", onto.strip()]
    elif step in ("continue", "abort", "skip"):
        args = ["rebase", f"--{step}"]
    else:
        raise ValueError("action должен быть start, continue, abort или skip")
    text = _run(args, token=token, check=False)
    return {"repo": url, "action": step, "output": gitops.truncate(text.strip())}


@mcp.tool()
def tag(name: str = "", action: str = "list", ref: str = "HEAD", message: str = "", push_tag: bool = False) -> dict:
    """Теги: list | create | delete (аннотированный, если есть message)."""
    url, token, _branch = _workspace()
    step = (action or "list").strip().lower()
    if step == "list":
        text = _run(["tag", "--list", "--sort=-creatordate"], token=token, check=False)
        return {"repo": url, "tags": _lines(text, 500)}
    target = (name or "").strip()
    if not target:
        raise ValueError("Не задано имя тега")
    if step == "create":
        args = ["tag", "-a", target, "-m", message.strip(), ref]
        if not message.strip():
            args = ["tag", target, ref]
        _run(args, token=token)
        if push_tag:
            _run(["push", "origin", target], token=token)
        return {"repo": url, "tag": target, "created": True, "pushed": bool(push_tag)}
    if step == "delete":
        _run(["tag", "-d", target], token=token)
        if push_tag:
            _run(["push", "origin", "--delete", target], token=token)
        return {"repo": url, "tag": target, "deleted": True, "remote": bool(push_tag)}
    raise ValueError("action должен быть list, create или delete")


@mcp.tool()
def run_git(args: list[str], timeout: int = 120) -> dict:
    """Произвольная git-команда: аргументы без слова `git` (полный доступ)."""
    url, token, _branch = _workspace()
    if not args:
        raise ValueError("Передайте аргументы git, например [\"status\", \"--short\"]")
    seconds = max(5, min(int(timeout or 120), 600))
    text = gitops.git_combined([str(item) for item in args], token=token, timeout=seconds)
    return {
        "repo": url,
        "args": [str(item) for item in args],
        "output": gitops.truncate(text) or "(пусто)",
    }


@mcp.tool()
def workspace_reset(confirm: bool = False) -> dict:
    """Пересоздать рабочую копию с нуля. Несохранённые изменения теряются."""
    if not confirm:
        raise ValueError("Подтвердите операцию: confirm=true")
    url, token, branch = _workspace(force=True)
    return {
        "repo": url,
        "branch": gitops.current_branch(token) or branch,
        "head": _run(["rev-parse", "--short", "HEAD"], token=token, check=False).strip(),
        "reset": True,
    }



# ─────────────────────── GitHub API: pull requests ───────────────────────


def _repo_parts() -> tuple[str, str]:
    url = _cfg("REPO_URL")
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise RuntimeError("REPO_URL должен быть github.com/<owner>/<repo>")
    return match.group(1), match.group(2)


def _github(
    method: str,
    path: str,
    payload: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> Any:
    token = _cfg("GIT_TOKEN")
    if not token:
        raise RuntimeError("Не задан GIT_TOKEN")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "Content-Type": "application/json",
            "User-Agent": "runturu-github-cmcp/1.3",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"GitHub {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub недоступен: {exc.reason}") from exc
    if not body:
        return {}
    return json.loads(body.decode("utf-8", "replace"))


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    result = _github("POST", "/graphql", {"query": query, "variables": variables})
    if not isinstance(result, dict):
        raise RuntimeError("GitHub GraphQL: пустой ответ")
    errors = result.get("errors")
    if errors:
        text = "; ".join(
            str(item.get("message") or item)
            for item in errors
            if isinstance(item, dict) or isinstance(item, str)
        )
        raise RuntimeError(f"GitHub GraphQL: {text[:300]}")
    data = result.get("data")
    return data if isinstance(data, dict) else {}


PR_STATES = {"open", "closed", "all"}
MERGE_METHODS = {"merge", "squash", "rebase"}
REVIEW_EVENTS = {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
MAX_PATCH = 4000


def _pr_number(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Номер PR должен быть числом") from exc
    if number <= 0:
        raise RuntimeError("Номер PR должен быть положительным")
    return number


def _limit(value: Any, maximum: int = 100) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("limit должен быть числом") from exc
    return min(max(number, 1), maximum)


def _branch_name(value: Any, hint: str) -> str:
    name = str(value or "").strip()
    if (
        not BRANCH_RE.match(name)
        or ".." in name
        or name.endswith(("/", "."))
        or "/." in name
    ):
        raise RuntimeError(f"Некорректное имя ветки {hint}: нужен обычный git-бранч")
    return name


def _pr_summary(item: dict[str, Any]) -> dict[str, Any]:
    head = item.get("head") or {}
    base = item.get("base") or {}
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "draft": item.get("draft"),
        "merged": item.get("merged"),
        "user": (item.get("user") or {}).get("login"),
        "head": head.get("ref"),
        "head_sha": head.get("sha"),
        "base": base.get("ref"),
        "url": item.get("html_url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "mergeable": item.get("mergeable"),
        "mergeable_state": item.get("mergeable_state"),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
        "changed_files": item.get("changed_files"),
        "commits": item.get("commits"),
        "comments": item.get("comments"),
        "labels": [
            (label or {}).get("name") for label in (item.get("labels") or [])
        ],
        "requested_reviewers": [
            (user or {}).get("login") for user in (item.get("requested_reviewers") or [])
        ],
    }


@mcp.tool()
def pr_list(state: str = "open", limit: int = 20) -> dict:
    """Список pull request'ов репозитория (state: open|closed|all)."""
    owner, repo = _repo_parts()
    items = _github("GET", f"/repos/{owner}/{repo}/pulls?state={state}&per_page={min(max(limit, 1), 50)}")
    return {
        "items": [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "user": (item.get("user") or {}).get("login"),
                "head": (item.get("head") or {}).get("ref"),
                "base": (item.get("base") or {}).get("ref"),
                "url": item.get("html_url"),
            }
            for item in items
        ]
    }


@mcp.tool()
def pr_comments(pr_number: int) -> dict:
    """Комментарии pull request'а: обычные и review-комментарии к коду."""
    owner, repo = _repo_parts()
    issue_comments = _github("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=50")
    review_comments = _github("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/comments?per_page=50")
    return {
        "comments": [
            {
                "kind": "issue",
                "id": item.get("id"),
                "user": (item.get("user") or {}).get("login"),
                "created_at": item.get("created_at"),
                "body": item.get("body"),
            }
            for item in issue_comments
        ]
        + [
            {
                "kind": "review",
                "id": item.get("id"),
                "user": (item.get("user") or {}).get("login"),
                "created_at": item.get("created_at"),
                "path": item.get("path"),
                "line": item.get("line") or item.get("original_line"),
                "body": item.get("body"),
            }
            for item in review_comments
        ]
    }


@mcp.tool()
def pr_comment(pr_number: int, body: str) -> dict:
    """Оставить комментарий в pull request от имени владельца токена."""
    text = (body or "").strip()
    if len(text) < 2:
        raise RuntimeError("Пустой комментарий")
    owner, repo = _repo_parts()
    result = _github(
        "POST",
        f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
        {"body": text[:60000]},
    )
    return {
        "id": result.get("id"),
        "url": result.get("html_url"),
        "created_at": result.get("created_at"),
    }



@mcp.tool()
def pr_get(pr_number: int) -> dict:
    """Детали pull request'а: состояние, draft, mergeable, ветки, статистика, ревьюеры."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    item = _github("GET", f"/repos/{owner}/{repo}/pulls/{number}")
    result = _pr_summary(item)
    result.update(
        {
            "body": item.get("body"),
            "node_id": item.get("node_id"),
            "merged_at": item.get("merged_at"),
            "merged_by": (item.get("merged_by") or {}).get("login"),
            "merge_commit_sha": item.get("merge_commit_sha"),
            "closed_at": item.get("closed_at"),
            "assignees": [
                (user or {}).get("login") for user in (item.get("assignees") or [])
            ],
        }
    )
    return result


@mcp.tool()
def pr_files(pr_number: int, limit: int = 100) -> dict:
    """Файлы, изменённые в pull request'е: статус, строки, patch (обрезается)."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    items = _github(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{number}/files?per_page={_limit(limit)}",
    )
    files = []
    for item in items:
        patch = item.get("patch") or ""
        files.append(
            {
                "filename": item.get("filename"),
                "previous_filename": item.get("previous_filename"),
                "status": item.get("status"),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "changes": item.get("changes"),
                "patch": patch[:MAX_PATCH],
                "patch_truncated": len(patch) > MAX_PATCH,
            }
        )
    return {"files": files}


@mcp.tool()
def pr_commits(pr_number: int, limit: int = 50) -> dict:
    """Коммиты pull request'а: SHA, первая строка сообщения, автор, дата."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    items = _github(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{number}/commits?per_page={_limit(limit)}",
    )
    commits = []
    for item in items:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        commits.append(
            {
                "sha": item.get("sha"),
                "message": (commit.get("message") or "").splitlines()[0][:300],
                "author": author.get("name"),
                "date": author.get("date"),
                "url": item.get("html_url"),
            }
        )
    return {"commits": commits}


@mcp.tool()
def pr_reviews(pr_number: int) -> dict:
    """Отзывы pull request'а: APPROVED, CHANGES_REQUESTED, COMMENTED и их авторы."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    items = _github(
        "GET", f"/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"
    )
    return {
        "reviews": [
            {
                "id": item.get("id"),
                "user": (item.get("user") or {}).get("login"),
                "state": item.get("state"),
                "body": item.get("body"),
                "commit_id": item.get("commit_id"),
                "submitted_at": item.get("submitted_at"),
            }
            for item in items
        ]
    }


@mcp.tool()
def pr_checks(pr_number: int) -> dict:
    """Проверки CI pull request'а: общий статус, статусы и check-runs."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    pr = _github("GET", f"/repos/{owner}/{repo}/pulls/{number}")
    sha = (pr.get("head") or {}).get("sha") or ""
    result: dict[str, Any] = {
        "head_sha": sha,
        "state": None,
        "statuses": [],
        "check_runs": [],
        "errors": [],
    }
    if not sha:
        result["errors"].append("не удалось определить head SHA")
        return result
    try:
        status = _github("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
        result["state"] = status.get("state")
        result["statuses"] = [
            {
                "context": item.get("context"),
                "state": item.get("state"),
                "description": item.get("description"),
                "url": item.get("target_url"),
            }
            for item in (status.get("statuses") or [])[:50]
        ]
    except RuntimeError as exc:
        result["errors"].append(f"statuses: {exc}")
    try:
        runs = _github(
            "GET",
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100",
        )
        result["check_runs"] = [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "url": item.get("html_url"),
            }
            for item in (runs.get("check_runs") or [])[:50]
        ]
    except RuntimeError as exc:
        result["errors"].append(f"check-runs: {exc}")
    return result


@mcp.tool()
def pr_create(
    title: str,
    head: str,
    base: str = "",
    body: str = "",
    draft: bool = False,
) -> dict:
    """Создать pull request: head — ветка-источник, base — целевая (по умолчанию DEFAULT_BRANCH)."""
    text = (title or "").strip()
    if len(text) < 3:
        raise RuntimeError("Слишком короткий заголовок PR")
    source = _branch_name(head, "head")
    target = _branch_name(base or _cfg("DEFAULT_BRANCH", "main"), "base")
    owner, repo = _repo_parts()
    payload: dict[str, Any] = {
        "title": text[:250],
        "head": source,
        "base": target,
        "draft": bool(draft),
    }
    if (body or "").strip():
        payload["body"] = str(body).strip()[:60000]
    item = _github("POST", f"/repos/{owner}/{repo}/pulls", payload)
    return _pr_summary(item)


@mcp.tool()
def pr_update(
    pr_number: int,
    title: str | None = None,
    body: str | None = None,
    base: str | None = None,
    state: str | None = None,
) -> dict:
    """Изменить PR: заголовок, описание, целевую ветку или состояние (open|closed)."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    payload: dict[str, Any] = {}
    if title is not None:
        text = str(title).strip()
        if len(text) < 3:
            raise RuntimeError("Слишком короткий заголовок PR")
        payload["title"] = text[:250]
    if body is not None:
        payload["body"] = str(body)[:60000]
    if base is not None:
        payload["base"] = _branch_name(base, "base")
    if state is not None:
        value = str(state).strip().lower()
        if value not in {"open", "closed"}:
            raise RuntimeError("state: open или closed")
        payload["state"] = value
    if not payload:
        raise RuntimeError("Нечего менять: передайте title, body, base или state")
    item = _github("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", payload)
    result = _pr_summary(item)
    result["body"] = item.get("body")
    return result


@mcp.tool()
def pr_merge(
    pr_number: int,
    method: str = "merge",
    commit_title: str = "",
    commit_message: str = "",
    sha: str = "",
    delete_branch: bool = False,
) -> dict:
    """Слить PR: method = merge | squash | rebase; delete_branch удаляет head-ветку после слияния."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    how = (method or "merge").strip().lower()
    if how not in MERGE_METHODS:
        raise RuntimeError("method: merge, squash или rebase")
    payload: dict[str, Any] = {"merge_method": how}
    if (commit_title or "").strip():
        payload["commit_title"] = str(commit_title).strip()[:250]
    if (commit_message or "").strip():
        payload["commit_message"] = str(commit_message).strip()[:60000]
    if (sha or "").strip():
        payload["sha"] = str(sha).strip()
    merged = _github("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge", payload)
    result: dict[str, Any] = {
        "merged": merged.get("merged"),
        "sha": merged.get("sha"),
        "message": merged.get("message"),
        "branch_deleted": False,
        "branch_delete_error": "",
    }
    if delete_branch:
        branch = ""
        try:
            pr = _github("GET", f"/repos/{owner}/{repo}/pulls/{number}")
            branch = (pr.get("head") or {}).get("ref") or ""
        except RuntimeError as exc:
            result["branch_delete_error"] = str(exc)
        if branch:
            try:
                _github(
                    "DELETE",
                    f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='')}",
                )
                result["branch_deleted"] = True
            except RuntimeError as exc:
                result["branch_delete_error"] = str(exc)
        elif not result["branch_delete_error"]:
            result["branch_delete_error"] = "не удалось определить head-ветку"
    return result


@mcp.tool()
def pr_review(
    pr_number: int,
    event: str = "COMMENT",
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
    commit_id: str = "",
) -> dict:
    """Отзыв на PR: event = APPROVE | REQUEST_CHANGES | COMMENT, comments — замечания к строкам ({path, line|position, body, side?})."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    action = (event or "COMMENT").strip().upper()
    if action not in REVIEW_EVENTS:
        raise RuntimeError("event: APPROVE, REQUEST_CHANGES или COMMENT")
    payload: dict[str, Any] = {"event": action, "body": str(body or "").strip()[:60000]}
    if (commit_id or "").strip():
        payload["commit_id"] = str(commit_id).strip()
    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(comments or []):
        if not isinstance(item, dict):
            raise RuntimeError(f"comments[{index}] должен быть объектом")
        path = str(item.get("path") or "").strip()
        text = str(item.get("body") or "").strip()
        if not path or len(text) < 2:
            raise RuntimeError(f"comments[{index}]: нужны path и body")
        comment: dict[str, Any] = {"path": path[:500], "body": text[:10000]}
        line = item.get("line")
        position = item.get("position")
        if line:
            comment["line"] = int(line)
            side = str(item.get("side") or "RIGHT").upper()
            comment["side"] = "LEFT" if side == "LEFT" else "RIGHT"
            if item.get("start_line"):
                comment["start_line"] = int(item["start_line"])
        elif position:
            comment["position"] = int(position)
        else:
            raise RuntimeError(f"comments[{index}]: нужен line или position")
        prepared.append(comment)
    if prepared:
        payload["comments"] = prepared[:50]
    if not payload["body"] and not prepared and action == "COMMENT":
        raise RuntimeError("Пустой отзыв: нужен body или comments")
    item = _github("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", payload)
    return {
        "id": item.get("id"),
        "state": item.get("state"),
        "url": item.get("html_url"),
        "submitted_at": item.get("submitted_at"),
    }


@mcp.tool()
def pr_review_comment(
    pr_number: int,
    body: str,
    path: str,
    line: int,
    side: str = "RIGHT",
    commit_id: str = "",
    start_line: int = 0,
) -> dict:
    """Комментарий к строке файла в PR; commit_id по умолчанию — head PR."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    text = str(body or "").strip()
    if len(text) < 2:
        raise RuntimeError("Пустой комментарий")
    file_path = str(path or "").strip()
    if not file_path:
        raise RuntimeError("Нужен path файла")
    try:
        position = int(line)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("line должен быть числом") from exc
    if position <= 0:
        raise RuntimeError("Нужен положительный номер строки line")
    sha = (commit_id or "").strip()
    if not sha:
        pr = _github("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        sha = (pr.get("head") or {}).get("sha") or ""
    if not sha:
        raise RuntimeError("Не удалось определить commit_id")
    side_value = str(side or "RIGHT").strip().upper()
    payload: dict[str, Any] = {
        "body": text[:10000],
        "commit_id": sha,
        "path": file_path[:500],
        "line": position,
        "side": "LEFT" if side_value == "LEFT" else "RIGHT",
    }
    if start_line:
        try:
            payload["start_line"] = int(start_line)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("start_line должен быть числом") from exc
        if payload["start_line"] <= 0:
            raise RuntimeError("start_line должен быть положительным")
    item = _github("POST", f"/repos/{owner}/{repo}/pulls/{number}/comments", payload)
    return {
        "id": item.get("id"),
        "url": item.get("html_url"),
        "path": item.get("path"),
        "line": item.get("line") or item.get("original_line"),
    }


@mcp.tool()
def pr_request_reviewers(
    pr_number: int,
    reviewers: list[str] | None = None,
    teams: list[str] | None = None,
) -> dict:
    """Запросить ревью у пользователей и/или команд."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    users = [str(item).strip() for item in (reviewers or []) if str(item).strip()]
    groups = [str(item).strip() for item in (teams or []) if str(item).strip()]
    if not users and not groups:
        raise RuntimeError("Передайте reviewers и/или teams")
    for login in users:
        if not USER_RE.match(login):
            raise RuntimeError(f"Некорректный логин GitHub: {login}")
    item = _github(
        "POST",
        f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers",
        {"reviewers": users[:15], "team_reviewers": groups[:15]},
    )
    return {
        "requested_reviewers": [
            (user or {}).get("login") for user in (item.get("requested_reviewers") or [])
        ],
        "requested_teams": [
            (team or {}).get("slug") for team in (item.get("requested_teams") or [])
        ],
    }


@mcp.tool()
def pr_draft(pr_number: int, draft: bool = True) -> dict:
    """Перевести PR в draft или вернуть в готовые к ревью (GitHub GraphQL)."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    pr = _github("GET", f"/repos/{owner}/{repo}/pulls/{number}")
    node_id = pr.get("node_id")
    if not node_id:
        raise RuntimeError("GitHub не вернул node_id PR")
    if draft:
        query = (
            "mutation($id: ID!) { convertPullRequestToDraft("
            "input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
        )
        field = "convertPullRequestToDraft"
    else:
        query = (
            "mutation($id: ID!) { markPullRequestReadyForReview("
            "input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
        )
        field = "markPullRequestReadyForReview"
    data = _graphql(query, {"id": node_id})
    pull = (data.get(field) or {}).get("pullRequest") or {}
    return {"draft": pull.get("isDraft", bool(draft)), "node_id": node_id}


@mcp.tool()
def pr_labels(
    pr_number: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Метки PR: без аргументов — список; add/remove — добавить и снять метки."""
    owner, repo = _repo_parts()
    number = _pr_number(pr_number)
    adding = [str(item).strip()[:50] for item in (add or []) if str(item).strip()]
    removing = [str(item).strip() for item in (remove or []) if str(item).strip()]
    if adding:
        _github(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            {"labels": adding[:20]},
        )
    for label in removing[:20]:
        _github(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{number}/labels/{quote(label, safe='')}",
        )
    items = _github("GET", f"/repos/{owner}/{repo}/issues/{number}/labels?per_page=100")
    return {"labels": [item.get("name") for item in items]}


@mcp.tool()
def commit_comment(sha: str, body: str) -> dict:
    """Комментарий к коммиту — работает без pull request."""
    text = (body or "").strip()
    commit = (sha or "").strip()
    if len(text) < 2:
        raise RuntimeError("Пустой комментарий")
    if len(commit) < 7:
        raise RuntimeError("Нужен SHA коммита (7+ символов)")
    owner, repo = _repo_parts()
    result = _github(
        "POST",
        f"/repos/{owner}/{repo}/commits/{commit}/comments",
        {"body": text[:60000]},
    )
    return {"id": result.get("id"), "url": result.get("html_url"), "created_at": result.get("created_at")}


@mcp.tool()
def issue_create(title: str, body: str, labels: list[str] | None = None) -> dict:
    """Создать issue с отчётом ревью."""
    name = (title or "").strip()
    text = (body or "").strip()
    if len(name) < 3:
        raise RuntimeError("Слишком короткий заголовок issue")
    if len(text) < 5:
        raise RuntimeError("Слишком короткое тело issue")
    owner, repo = _repo_parts()
    payload: dict[str, Any] = {"title": name[:250], "body": text[:60000]}
    if labels:
        payload["labels"] = [str(item)[:50] for item in labels][:10]
    result = _github("POST", f"/repos/{owner}/{repo}/issues", payload)
    return {
        "number": result.get("number"),
        "url": result.get("html_url"),
        "state": result.get("state"),
        "created_at": result.get("created_at"),
    }


# ─────────────────────────── Приложение ───────────────────────────


async def health(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVER_NAME})


def create_app() -> Any:
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.add_middleware(ConfigMiddleware)
    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8080")),
        log_level=os.environ.get("MCP_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
