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
        "force) и произвольные git-команды. Репозиторий и токен задаются в "
        "настройках подключения."
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


def _github(method: str, path: str, payload: dict | None = None) -> Any:
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
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "runturu-github-cmcp/1.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"GitHub {exc.code}: {detail}") from exc


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
