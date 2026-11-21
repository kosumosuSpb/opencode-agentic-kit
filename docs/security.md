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
git -c core.fsmonitor=false --no-pager status
git -c core.fsmonitor=false --no-pager status --short
git -c core.fsmonitor=false --no-pager status --short --branch
git -c core.fsmonitor=false --no-pager status --porcelain
git ls-tree -r --name-only HEAD
git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv
git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --stat
git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --cached
git -c core.fsmonitor=false --no-pager diff --no-ext-diff --no-textconv --cached --stat
git --no-pager log --oneline -n 20
git --no-pager log --oneline -5
git --no-pager branch --show-current
git rev-parse --show-toplevel
```

Две команды, которые легко пропустить при ручном перечислении, —
`git -c core.fsmonitor=false --no-pager status --short --branch` и `git ls-tree -r --name-only HEAD`.

OpenCode scanner передаёт plugin отдельный resource для каждой shell-подкоманды.
Цепочка разрешается автоматически, только если каждая часть входит в allowlist.
Opaque syntax, wrappers, aliases, substitutions и неизвестные аргументы не
нормализуются Kit.

Developer и Reviewer дополнительно могут выполнять `git diff` отдельного pathspec.
Для auto-allow обязательна точная глобальная часть `-c core.fsmonitor=false
--no-pager diff`, затем одновременно `--no-ext-diff` и `--no-textconv`. Допустимы
`--cached` и `--stat`, а pathspec должен идти после обязательного `--`. Эти защиты
отключают pager, fsmonitor hook, внешний diff helper и исполняемый textconv driver.
Дополнительные Git options не нормализуются и не могут переопределить защиты.

Команды `git status` защищены от `core.fsmonitor`; для `git log` и `git branch`
отключён внешний pager. Старые незащищённые варианты больше не разрешаются автоматически.

## Runner allowlist

Session scope привязывает Runner к абсолютному Git root текущей сессии. Developer
получает только формы TDD, `check`, baseline, evidence и `gate fast`, переданные `/implement`.
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
