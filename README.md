# Github — CMCPT для доступа к репозиторию

MCP-сервер Runturu с **полным доступом к одному git-репозиторию GitHub**:
рабочая копия, чтение файлов и истории, коммиты, ветки, merge/rebase, push
(включая force) и произвольные git-команды.

- MCP endpoint: `/mcp` (streamable HTTP)
- health: `/health`
- порт: `8080`
- манифест: [`runturu-mcp.json`](runturu-mcp.json)

## Настройки подключения

Задаются при создании CMCP; гейтвей передаёт их в заголовке
`X-Runturu-Config` (base64url JSON), сервер читает их на каждый запрос.

| Ключ | Тип | Обязательно | Что это |
| --- | --- | --- | --- |
| `REPO_URL` | url | да | `https://github.com/<owner>/<repo>` — только HTTPS и только GitHub |
| `GIT_TOKEN` | password | да | GitHub PAT: contents, pull requests, issues (см. раздел о правах ниже) |
| `DEFAULT_BRANCH` | string | нет | ветка при первом клонировании, по умолчанию `main` |
| `GIT_USER_NAME` | string | нет | автор коммитов, по умолчанию `Runturu Agent` |
| `GIT_USER_EMAIL` | string | нет | почта автора коммитов, по умолчанию `agent@runturu.com` |

Токен нужен с доступом к выбранному репозиторию и правами на запись, если
агент будет коммитить, пушить, сливать PR или оставлять отзывы.

### Fine-grained PAT (рекомендуется)

При создании токена выберите **Only select repositories** и нужный репозиторий
(иначе GitHub отвечает `404`, а не `403`). Repository permissions:

| Разрешение | Уровень | Зачем |
| --- | --- | --- |
| **Contents** | Read and write | clone/fetch/push, ветки и коммиты, слияние PR (`PUT /pulls/{n}/merge` требует Contents: write), удаление head-ветки |
| **Pull requests** | Read and write | список/детали/файлы/коммиты/отзывы; создание, изменение и закрытие PR, ревью, комментарии к строкам, запрос ревьюеров, draft ↔ ready |
| **Issues** | Read and write | `pr_comment` (обсуждение PR), `pr_labels`, `issue_create` |
| **Commit statuses** | Read | `pr_checks`: общий CI-статус коммита |
| **Metadata** | Read | обязательное, GitHub добавляет сам |
| **Workflows** | Read and write | только если агент пушит изменения в `.github/workflows/` |

Важно: **Checks: Read в fine-grained PAT нет**. GitHub отдаёт `check-runs`
только токенам GitHub App и классическим PAT с `repo`. Поэтому с fine-grained
токеном `pr_checks` вернёт combined statuses, а `check_runs` будет пустым,
с текстом ошибки в `errors[]`. Для полного CI (check-runs) нужен classic PAT.

Чисто на чтение (агент только смотрит PR, ничего не пишет): `Contents: Read`,
`Pull requests: Read`, `Issues: Read`, `Commit statuses: Read` — этого хватит
для `repo_info`/`pr_list`/`pr_get`/`pr_files`/`pr_checks`, но не для ревью и
комментариев.

### Classic PAT

| Scope | Зачем |
| --- | --- |
| `repo` | всё: содержимое, ветки, PR, issues, комментарии, статусы, check-runs, merge |
| `workflow` | если агент меняет файлы `.github/workflows/` (без него git push таких файлов отклоняется) |
| `public_repo` | только публичные репозитории, если приватные не нужны |

Токен должен принадлежать пользователю с правом записи в репозиторий. Для
организаций fine-grained PAT может требовать одобрения владельца организации.
Ставьте срок действия и выдавайте отдельный токен на репозиторий.

## Инструменты

| Инструмент | Что делает |
| --- | --- |
| `repo_info` | ветка, HEAD, изменения, размер, remote |
| `list_files` | файлы рабочей копии или ревизии |
| `read_file` | файл из рабочей копии или ревизии |
| `git_log` | история коммитов (по ветке и/или файлу) |
| `git_diff` | рабочая копия ↔ HEAD, между ревизиями, staged |
| `git_show` | коммит целиком или файл в ревизии |
| `git_blame` | авторство строк |
| `list_branches` | локальные и удалённые ветки |
| `list_tags` | теги |
| `write_file` | создать/перезаписать файл (без коммита) |
| `delete_file` | удалить файл (без коммита) |
| `commit` | закоммитить всё или указанные пути, `amend` |
| `push` | push ветки/тегов, `force` = `--force-with-lease`, есть `dry_run` |
| `repo_sync` | fetch + pull (`--ff-only` или `--rebase`) |
| `create_branch` | создать ветку от HEAD или ревизии |
| `checkout` | переключиться на ветку/тег/коммит |
| `delete_branch` | удалить ветку локально и/или в origin |
| `reset` | `soft` / `mixed` / `hard` на ревизию |
| `merge` | слить ревизию в текущую ветку |
| `rebase` | rebase, `continue` / `abort` / `skip` |
| `tag` | создать/удалить/список тегов |
| `run_git` | произвольная git-команда (escape hatch) |
| `workspace_reset` | пересоздать рабочую копию с нуля |
| `pr_list` | список PR (`open`/`closed`/`all`) |
| `pr_get` | детали PR: draft, mergeable, ветки, статистика, ревьюеры |
| `pr_files` | изменённые файлы и patch |
| `pr_commits` | коммиты в PR |
| `pr_reviews` | отзывы: APPROVED / CHANGES_REQUESTED / COMMENTED |
| `pr_checks` | CI: общий статус, статусы, check-runs |
| `pr_create` | создать PR (`head` → `base`, draft) |
| `pr_update` | изменить заголовок/описание/base, открыть или закрыть |
| `pr_merge` | слить PR (`merge`/`squash`/`rebase`), удалить ветку |
| `pr_review` | отзыв с замечаниями к строкам |
| `pr_review_comment` | комментарий к конкретной строке файла |
| `pr_request_reviewers` | запросить ревью у пользователей/команд |
| `pr_draft` | перевести в draft или вернуть к ревью |
| `pr_labels` | посмотреть, добавить или снять метки |
| `pr_comments` | обычные и review-комментарии PR |
| `pr_comment` | комментарий в обсуждении PR |
| `commit_comment` | комментарий к коммиту без PR |
| `issue_create` | issue с отчётом ревью |

## Работа с pull request

Типовой сценарий агента:

1. `create_branch(name="feature/x")` → `write_file` → `commit` → `push`;
2. `pr_create(title, head="feature/x", base="main", body)` — открыть PR;
3. `pr_files` + `pr_get` — изучить diff и состояние;
4. `pr_review(pr_number, event="REQUEST_CHANGES", body, comments=[{path, line, body}])`
   или `pr_review_comment` — оставить замечания к строкам;
5. `pr_update(state="closed")` / `pr_draft(draft=False)` — управлять состоянием;
6. `pr_merge(pr_number, method="squash", delete_branch=True)` — слить и убрать ветку.

`pr_checks` показывает CI, `pr_request_reviewers` зовёт ревьюеров, `pr_labels`
управляет метками. `pr_comment` — обычный комментарий в обсуждении,
`pr_review` — именно отзыв (approve / request changes) с замечаниями.

## Как это работает

1. При первом вызове сервер делает `git init` + `git fetch` из
   `REPO_URL` и переключается на `DEFAULT_BRANCH`.
2. Рабочая копия живёт в `/tmp/cmcp-github/repo` — **внутри контейнера**.
   Она пропадает при пересоздании контейнера (активация, смена версии,
   перезапуск хоста) и создаётся заново при следующем вызове. Незакоммиченные
   изменения при этом теряются, запушенное — нет.
3. Токен передаётся git через `GIT_CONFIG_*` как `http.https://github.com/.extraheader`,
   поэтому не попадает в argv, в `.git/config` и в `git remote -v`.
   В любом выводе инструментов токен и его base64 маскируются в `***`.
4. Пути файлов проверяются: абсолютные пути и `..` за пределы репозитория
   запрещены, символические ссылки разрешаются.

## Ограничения

- Контейнер: 512 МБ памяти, 0.5 CPU, 128 процессов. Очень большие репозитории
  (>200 МБ) клонировать не получится — лимит платформы.
- Сеть: только `github.com` по HTTPS через egress-прокси Runturu.
  Git-хостинги кроме GitHub, SSH и локальные пути недоступны.
- Инструменты `push(force=true)`, `reset(mode="hard")`, `delete_branch`,
  `rebase`, `workspace_reset` и `run_git` — разрушительные: могут потерять
  работу. Это осознанный выбор владельца шаблона, агент должен предупреждать
  о них пользователя.

## Локальный запуск

```bash
pip install -r requirements.txt
export REPO_URL=https://github.com/owner/repo
export GIT_TOKEN=...
python -m uvicorn server.main:app --port 8080
```

```bash
curl -s localhost:8080/health
curl -s localhost:8080/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Тесты

```bash
python -m pytest -q
```

Тесты не ходят в сеть: проверяются нормализация адреса, защита путей,
маскирование токена и реальные git-операции в локальном временном репозитории.
