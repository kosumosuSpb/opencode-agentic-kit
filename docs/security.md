# Permissions, shell policy и безопасность

Kit ограничивает роли через agent rules и session-scoped permissions plugin. Это
процедурная защита внутри одного OpenCode/OS-пользователя, а не системный sandbox.

## Границы ролей

- **Architect** читает проект, но изменяет только draft spec/plan активной задачи.
- **Developer** изменяет исходники, тесты и конфигурацию текущего Git-проекта после
  approval; approved documents, drafts и `.git/**` остаются закрыты.
- **Reviewer** работает read-only и использует свежий VERIFY/FULL context.
- Все роли получают `external_directory: deny` и не должны читать secrets (`.env`,
  SSH materials, API keys).

Developer не может изменять `.agentic/drafts/*`, `.agentic/tasks/*/drafts/**`,
`specs/approved.md`, `plans/approved.md`, task-specific approved documents или `.git/**`.
Для чтения дерева и содержимого роли используют встроенные `read`, `glob` и `grep`;
shell-команды `ls`, `find`, `cat`, `sed` и shell-`grep` не входят в общий allowlist.

OpenCode применяет правила по порядку; последнее совпадение побеждает. Plugin добавляет
целостный session scope и затем повторно запрещает protected paths. Marker и metadata
позволяют заменить только scope, которым владеет Kit, не удаляя чужие session rules.

## Read-only shell allowlist

Plugin автоматически разрешает только точные команды:

```text
pwd
git status
git status --short
git status --short --branch
git status --porcelain
git ls-tree -r --name-only HEAD
git diff --stat
git diff --no-ext-diff
git diff --no-ext-diff --stat
git diff --no-ext-diff --cached
git diff --no-ext-diff --cached --stat
git --no-pager log --oneline -n 20
git log --oneline -5
git branch --show-current
git rev-parse --show-toplevel
```

Две команды, которые легко пропустить при ручном перечислении, —
`git status --short --branch` и `git ls-tree -r --name-only HEAD`.

OpenCode scanner передаёт plugin отдельный resource для каждой shell-подкоманды.
Цепочка разрешается автоматически, только если каждая часть входит в allowlist.
Opaque syntax, wrappers, aliases, substitutions и неизвестные аргументы не
нормализуются Kit.

Reviewer дополнительно может выполнять `git diff` с комбинациями `--no-ext-diff`,
`--cached`, `--stat` только после обязательного `-- <pathspec>`.

## Runner allowlist

Session scope привязывает Runner к абсолютному Git root текущей сессии. Developer
получает только формы TDD, baseline, evidence и `gate fast`, переданные `/implement`.
Reviewer получает evidence, ready/review-context и ограниченный `review-submit`.
Произвольный project path, `--help`, неизвестные Runner-команды и FULL от Developer не
разрешаются автоматически.

Runner TDD принимает pytest/unittest напрямую, через `python -m` или через
`uv run --locked`. Команда запускается argv-массивом с `shell=False`; `--expect`
передаётся одним аргументом. `$()`, backticks, redirection и незакавыченные shell-
операторы не получают auto-allow.

## Ask и deny

Различие намеренное:

- `git commit` и неизвестная shell-команда получают **ask** — требуется отдельное
  решение пользователя;
- `git push` и package publication (`uv publish`, `twine upload`, `npm publish`,
  `pnpm publish`, `cargo publish`) получают **deny**;
- распознанные deployment-команды (`kubectl`, `helm`, `terraform`/`tofu`, push образов,
  deploy через `make`/`just`/npm и `ansible-playbook`) получают **deny**;
- shell-запись через redirection, `touch`, `tee`, `cp`, `mv`, `sed -i` или `perl -pi`
  получает **deny**: Developer должен использовать edit/write;
- более широкий inherited allow для неизвестной команды понижается до **ask**, если
  команда не попала под явный deny.

Таким образом, deployment не относится к обычному `ask`: известные deployment-формы
явно запрещены. Неизвестная команда может потребовать подтверждение, но это не делает
её безопасной или поддерживаемой.

## Threat model

- Approval и permissions не защищают от другого процесса того же пользователя.
- Reviewer не запускает произвольные тесты: исполняемый код не является read-only.
- Для hostile code используй отдельного Linux-пользователя, контейнер с подходящей
  изоляцией или CI runner.
- Kit не должен получать секреты из `.env`, SSH-каталогов или agent prompts.
- Commit, push, deploy и publication остаются отдельной ответственностью человека.
