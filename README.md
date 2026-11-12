# OpenCode Agentic Kit v1.2

Локальный Agentic Kit для OpenCode 2.2.7: роли Architect, Developer и Reviewer,
human approval, внешний state store, TDD evidence и Python Quality Gates. Версия
1.2 поддерживает несколько **последовательных** задач в одном Git-проекте и
консервативное автоматическое разрешение read-only shell-команд.

## Требования

- Linux, Git и Python 3.11 или новее.
- OpenCode 2.2.7 для реальной интеграционной проверки.
- Оригинальный навык `ub-mode` в пользовательской установке агента; Kit его не
  копирует и не меняет.
- Node.js 24 только для JavaScript-теста. `npm` и глобальный пакет `typescript` не требуются.
- Approval — процедурный soft lock, а не OS-изоляция. Для недоверенного кода нужен
  отдельный Linux-пользователь или CI runner.
- Параллельная реализация задач и автоматическое слияние конфликтов не входят в v1.2.

## Главное в v1.2

- Добавлена команда `/task`: `new <id>`, `use <id>` и `status`.
- Drafts, approved documents, baseline, TDD и receipts разделены по стабильному task ID.
- `/finalize` после свежего full gate фиксирует `completed_at`, не удаляя историю.
- Старый single-task проект автоматически принимается как задача `legacy` без
  перемещения прежних документов и state.
- Receipt содержит `task_id`; approval или receipt одной задачи не подходят другой.
- OpenCode 2.2.7 сам разбирает shell AST, а plugin разрешает цепочку только когда
  каждый извлечённый resource входит в точный read-only allowlist.
- `/approve` по-прежнему отправляет synthetic message с `resume: false`; Developer
  запускается только явным `/implement`.

## Проверка канонического репозитория (LMDE 7)

Все команды ниже выполняются **из основного репозитория**, не из временной копии:

```bash
cd "$HOME/dev/own/opencode-agentic-kit"
python3 -m unittest discover -s tests -v
nvm exec 24 node tests/test_plugin.cjs
python3 install.py          # только план, без изменений
```

## Установка, обновление, откат

**Не запускай установку/обновление одновременно с работающими сессиями OpenCode.**

```bash
cd "$HOME/dev/own/opencode-agentic-kit"
python3 install.py --install   # первая установка, только с явного согласия
python3 install.py --upgrade   # обновление существующей v1.2-preview.1 (сохранит первый backup)
python3 install.py --rollback  # восстановит состояние до ПЕРВОЙ preview-установки
```

`--install` больше не выдаёт ложное `ALREADY_INSTALLED` после изменения исходников: если установка уже существует и содержимое отличается от канонического репозитория, команда блокируется с `UPGRADE_REQUIRED`. `--upgrade` допускается только при совпадении текущих установленных файлов с manifest. Локальные изменения установки не перезаписываются.

**Важно:** имя установленного каталога `opencode-agentic/preview/` и ID
`agentic.workflow.preview` сохранены только для совместимости с прежней установкой.
Они не являются каталогом исходников и не означают отдельную preview-сборку.

В отдельном терминале открой тестовый проект и проверь наличие шести команд:

```bash
cd ~/dev/own/my-test-project
opencode mini
```

Для проверки разрешения проекта и корректного блокирования до approval:

```bash
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" check-approval
```

До `/design` или `/task new` ожидается `BLOCKED: NO_ACTIVE_TASK`; после создания
задачи, но до approval — `BLOCKED: NO_APPROVAL`. Это защитное поведение.

## Задачи и workflow

Команда `/task` имеет три формы:

```text
/task status
/task new add-health-endpoint
/task use add-health-endpoint
```

ID состоит из 1–64 ASCII-букв, цифр, `.`, `_` и `-`, начинается с буквы или
цифры. Если новый проект начинает сразу с `/design`, автоматически создаётся
`task-1`. Проект, созданный до v1.2, автоматически принимается как `legacy`.

Для новой задачи используются отдельные пути:

```text
.agentic/tasks/<id>/drafts/{spec,plan}.md
specs/tasks/<id>/approved.md
plans/tasks/<id>/approved.md
$AGENTIC_STATE_DIR/projects/<project-id>/tasks/<id>/
```

Project ID вычисляется из абсолютного Git top-level, поэтому разные репозитории и
worktree-пути не делят state.

Пример двух последовательных задач:

```text
/task new task-a
/design Спроектировать задачу A
/approve
/implement Реализовать задачу A
/review Проверить задачу A
/finalize Подготовить заключение и завершить задачу A

/task new task-b
/design Спроектировать задачу B
/approve
/implement Реализовать задачу B
/review Проверить задачу B
/finalize Подготовить заключение и завершить задачу B
```

`/approve` фиксирует snapshot активной задачи, но не запускает модель. До approval
задача B получает `NO_APPROVAL`; approval A не подходит. `/finalize` выполняет full
gate, проверяет receipt и записывает `completed_at`. История не удаляется. Вернуться
к A можно через `/task use task-a`; её документы и `evidence` остаются доступны,
но повторная реализация завершённой задачи блокируется `TASK_COMPLETED`.

## Approval и receipts

Baseline содержит task-specific копии spec/plan. `check-approval` сравнивает SHA-256
с approved documents; изменение даёт `SPEC_CHANGED`. Receipt содержит `task_id`,
baseline hashes и fingerprint Git/worktree. Runner проверяет все три связи, поэтому
старый full/verify receipt не подтверждает новую задачу, а повреждение state одной
задачи не создаёт PASS для другой.

```bash
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" task-status
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" evidence
```

## TDD и Quality Gates

Для проверяемого TDD-журнала Developer запускает каждый этап через Runner:

```bash
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" tdd --cycle normalize-empty --stage red \
  --expect "новый тест отклоняет пустое имя" -- python -m pytest -q tests/test_name.py
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" tdd --cycle normalize-empty --stage green -- python -m pytest -q tests/test_name.py
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" evidence
```

Runner принимает только pytest/unittest напрямую, через `python -m` или
`uv run --locked`. Verify/Full собирают обычную wheel без скачивания зависимостей,
устанавливают её отдельно и запускают тесты вне source tree. PASS receipt подтверждает
конкретные task ID, baseline и fingerprint, но не заменяет доказательство каждого
acceptance criterion.

## Shell permissions

OpenCode 2.2.7 сначала разбирает команду штатным scanner и передаёт permission hook
отдельный resource для каждой подкоманды. Собственного Bash-парсера в Kit нет.
Auto-allow применяется только к точному списку:

```text
pwd
git status
git status --short
git status --porcelain
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

Поэтому автоматически разрешаются `git status && git diff --stat` и
`git status --short && git log --oneline -5`. Встроенные `read`, `glob` и `grep`
также разрешены и предпочтительны для просмотра файлов.

`git commit`, deploy и неизвестные команды получают `ask`. `git push` и package
publish получают `deny`. Смешанная цепочка с любой неразрешённой частью не получает
auto-allow. Redirections, wrappers, aliases, substitutions и неизвестные аргументы
не нормализуются Kit и остаются `ask`; opaque syntax блокирует scanner OpenCode.
Более широкий родительский `allow` понижается до `ask`, а существующий `deny` не
ослабляется. Scanner не передаёт hook тип соединяющего оператора, поэтому pipeline
из одних allowlisted read-only частей также безопасно разрешён; любая иная часть
возвращает весь запрос в `ask`.

По умолчанию модели не изменены: `openai/gpt-5.6-sol` для Architect/Reviewer и
`opencode/deepseek-v4.1-flash-free` для Developer. Переменные
`AGENTIC_ARCHITECT_MODEL`, `AGENTIC_DEVELOPER_MODEL`, `AGENTIC_REVIEWER_MODEL`
принимают замену в формате `provider/model`.

## Оставшиеся ограничения

- Только последовательные задачи; нет параллельного scheduler или merge-сервера.
- Approval остаётся soft lock одного OS-пользователя.
- Auto-allow намеренно ограничен точными командами; другие безопасные варианты могут
  потребовать подтверждение.
- Wheel-проверка требует локально доступных build backend и зависимостей и ничего не
  скачивает (`--no-build-isolation`, `--no-deps`).
- Профили Go, JS/TS, Rust, Kotlin и C# не добавлены.
- Installer не меняет `opencode.jsonc`, не создаёт коммиты и не выполняет push,
  deploy или публикацию.

## Тесты v1.2

- Python unittest: legacy migration, последовательный A/B workflow, независимые
  approvals/receipts, hashes, symlink-защита, TDD, wheel, installer и project/worktree
  state separation.
- Node 24 mock plugin: `resume: false`, явный `/implement`, `/task`, guards,
  позитивные и негативные shell permissions.
- Статическая проверка Python: `py_compile`.
- Реальный OpenCode 2.2.7 проверяется только в изолированных `HOME`/`XDG_*`.
