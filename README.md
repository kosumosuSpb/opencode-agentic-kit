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

- Добавлена команда `/task`: `new <id>`, `use <id>`, `status` и аудируемый `reopen <id>`.
- Drafts, approved documents, baseline, TDD и receipts разделены по стабильному task ID.
- `/finalize` после свежего FULL gate запускает независимый Reviewer, но не принимает
  работу. Только отдельный `/accept <review-id>` после PASS-заключения фиксирует `completed_at`.
- Старый single-task проект автоматически принимается как задача `legacy` без
  перемещения прежних документов и state.
- Receipt содержит `task_id`; approval или receipt одной задачи не подходят другой.
- OpenCode 2.2.7 сам разбирает shell AST, а plugin разрешает цепочку только когда
  каждый извлечённый resource входит в точный read-only allowlist.
- `/approve` по-прежнему отправляет synthetic message с `resume: false`; Developer
  запускается только явным `/implement`.
- `/task new`, `/task use` и `/task status` показывают результат в `opencode mini`
  через companion TUI toast без запуска модели.
- После approval plugin добавляет session-scoped permissions Developer поверх
  унаследованных agent rules: исходники, тесты и конфигурация внутри текущего
  Git-проекта доступны для edit/write, а protected documents и `.git` остаются deny.

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

В отдельном терминале открой тестовый проект и проверь наличие семи команд:

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

Команда `/task` имеет четыре формы:

```text
/task status
/task new add-health-endpoint
/task use add-health-endpoint
/task reopen add-health-endpoint
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
/review Проверить задачу A  # дождаться независимого VERIFY-заключения
/finalize Подготовить независимое заключение по задаче A
# дождаться итоговой проверки новых FULL evidence и прочитать её
/accept <review-id, показанный /finalize>

/task new task-b
/design Спроектировать задачу B
/approve
/implement Реализовать задачу B
/review Проверить задачу B
/finalize Подготовить независимое заключение по задаче B
# дождаться финального ответа Reviewer и прочитать его
/accept <review-id, показанный /finalize>
```

`/approve` фиксирует snapshot активной задачи, но не запускает модель. Approved-копия
получает машинно-управляемый статус `APPROVED — утверждено человеком`; исходный
черновик остаётся неизменным. Если в остальном тексте остаются `DRAFT`, «требует
утверждения» или «не является утверждённым», Runner блокирует approval и просит
исправить черновик. До approval
задача B получает `NO_APPROVAL`; approval A не подходит. `/review` выполняет VERIFY
gate, создаёт Review ID, привязанный к task ID, approval hashes, fingerprint и VERIFY
receipt, и асинхронно запускает полноценное независимое ревью исходников. Reviewer
сохраняет verdict и конкретные структурированные findings. `/finalize` выполняет FULL
gate и создаёт итоговый Review ID. При неизменных code и approval он переиспользует
сохранённое независимое ревью и проверяет только новые FULL/mutation evidence и
carried findings. При изменении task/approval/fingerprint старый review становится
`stale`, а `/finalize` требует полноценный актуальный FULL fallback review. Такой же
безопасный fallback используется, если `/review` не запускали. Reviewer обязан записать
`pass`, `reject` либо `exception`; ни Gate PASS, ни отправка prompt сами по себе не меняют
`completed_at`. После видимого PASS-заключения человек отдельно выполняет
`/accept <review-id>`. Runner повторно проверяет все связи и атомарно записывает
acceptance audit. Повторный `/accept` идемпотентен; rejected, exception, pending,
stale или superseded review блокируются. История не удаляется.
Major findings блокируют PASS и не считаются автоматически исправленными или принятыми.
Process exception хранится отдельным статусом и не превращается в фиктивный PASS.
Повторный `/finalize` при текущем `pending`/`passed` итоговом review не перезапускает FULL gate
и не создаёт второго Reviewer; после `reject`, `exception` или `stale` создаётся новый цикл.

Если старая версия преждевременно записала `completed_at` без acceptance audit, не
редактируй `tasks.json` вручную. Человек может выполнить `/task reopen <id>`: Runner
сохранит прежний timestamp и прежний acceptance (если был) в `status_history`, а
TDD, receipts и review records — в `status-history/<stamp>-reopen/`; затем очистит
только текущий completion status, инвалидирует текущие VERIFY/FINALIZE pointers,
выберет задачу активной и потребует новый `/review` либо полноценный fallback
`/finalize` → Reviewer → `/accept`. Для исторической `task-b` эту команду следует
запускать только при решении продолжить её приёмку; installer сам её не выполняет.

## Approval и receipts

Baseline содержит task-specific копии spec/plan. `check-approval` сравнивает SHA-256
с approved documents; изменение даёт `SPEC_CHANGED`. Receipt содержит `task_id`,
baseline hashes и fingerprint Git/worktree. Runner проверяет все три связи, поэтому
старый full/verify receipt не подтверждает новую задачу, а повреждение state одной
задачи не создаёт PASS для другой.

Уже существующий противоречивый approval нельзя исправлять ручным редактированием:
это нарушит SHA baseline. После исправления исходных draft-файлов человек явно
выполняет `/approve replace`. Runner сначала проверяет старые хеши, сохраняет прежние
baseline, approved documents, TDD и receipts в task-specific `approval-history/`,
а затем атомарно публикует новые approved-копии. Старые evidence остаются связаны
с прежним approval и не выдаются за доказательство нового baseline.

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
`uv run --locked`. Verify/Full собирают обычную wheel во временном каталоге без
скачивания зависимостей, устанавливают её в отдельный target, импортируют обнаруженные
в самом wheel top-level API через `python -I -S` и проверяют, что каждый
`__file__`/`__path__` находится внутри target. Затем копия project-owned тестов
запускается из временного каталога с target первым в `sys.path` и без source root.
Pytest и его runtime-зависимости копируются в отдельный временный toolsite, поэтому
даже project-local `.venv` не добавляется в import paths smoke-процесса. Kit не
содержит имён прикладных модулей или функций. PASS receipt подтверждает
конкретные task ID, baseline и fingerprint, но не заменяет доказательство каждого
acceptance criterion.

Перед переключением на Developer `/implement` вызывает штатный
`implement-context`. Runner одним JSON-ответом подтверждает active task и approval,
возвращает пути approved spec/plan, выбранный Python backend, результаты Python и
pytest probes и команду теста. Plugin на их основе передаёт точные формы RED,
GREEN, REFACTOR, `evidence` и `gate fast` уже с абсолютными путями Runner и
Git-проекта. Developer не должен запускать `--help`, пересчитывать approval-хеши
или повторять `python --version`/import/setuptools-диагностику через shell.

Перед Reviewer `/review` выполняет VERIFY, вызывает `review-context verify`, создаёт
phase=`verify` Review ID и сохраняет итоговое заключение с findings.
Контекст содержит active task, approved/baseline paths и hashes, стадии каждого
TDD cycle, freshness receipt, команды Gate, wheel hash и отдельные build/install/
import-provenance/public-API-smoke steps. Каждый факт связан с конкретным файлом внешнего state store.
Reviewer отличает evidence Runner от собственной read-only инспекции и не считает
отсутствие wheel в рабочем дереве дефектом: wheel создаётся во временном каталоге.
`/finalize` создаёт phase=`finalize` Review ID. Его mode равен
`reuse-independent-review` только при точном совпадении task/approval/fingerprint;
иначе используется `full-independent-review`. Reopen provenance и пути исторических
archive видны в `task_audit`, но исторические evidence не выдаются за актуальные.
Для структуры и содержимого Reviewer также использует `read`, `glob`, `grep`, а не
`ls`/`find` или произвольный запуск Python.

Обзор дерева и содержимого выполняется встроенными OpenCode 2.2.7 инструментами
`read`, `glob` и `grep`. `ls`, `find`, `cat`, `sed` и shell-`grep` намеренно не
добавлены в общий allowlist: это устраняет источник повторных запросов на уровне
поведения Developer, не расширяя поверхность shell.

## Developer edit/write permissions

OpenCode 2.2.7 объединяет agent rules из применимых конфигураций, а session-scoped
rules вычисляет после них; последнее совпавшее правило побеждает. Поэтому plugin
после успешного `implement-context` добавляет целостный Developer scope, а не пытается
перечислить возможные файлы проекта. Он разрешает новые и существующие исходники,
тесты и конфигурацию, в том числе корневые модули, и затем снова запрещает:

```text
.git/**
.agentic/drafts/*
.agentic/tasks/*/drafts/**
specs/approved.md
plans/approved.md
specs/tasks/*/approved.md
plans/tasks/*/approved.md
external_directory: *
```

`external_directory` сохраняет запрет других проектов и выхода через symlink.
При отказе write/edit Developer обязан остановиться и сообщить ошибку. Shell-запись
через redirection, `touch`, `tee`, `cp`, `mv`, `sed -i` и `perl -pi` блокируется
plugin и не является запасным способом редактирования.

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

После approval session scope дополнительно разрешает только привязанные к текущему
Git root штатные вызовы установленного Runner:

```text
python3 <runner> --project <current-root> tdd ...
python3 <runner> --project <current-root> evidence
python3 <runner> --project <current-root> gate fast
```

Read-only session scope Reviewer разрешает точные `evidence`, `ready verify/full`
и `review-context verify/full`, но не произвольные тестовые или Python-команды.
Reviewer сохраняет `edit: deny` и `external_directory: deny` для всех путей.

Runner сам валидирует допустимую тестовую команду. Другой project path, `gate full`,
`--help`, shell metacharacters и произвольный `python3 *` не получают auto-allow.

`git commit`, deploy и неизвестные команды получают `ask`. `git push`, package
publish и shell-попытки записи получают `deny`. Смешанная цепочка с любой
неразрешённой частью не получает auto-allow. Wrappers, aliases, substitutions и
неизвестные аргументы не нормализуются Kit и остаются `ask`; redirection получает
`deny`, а opaque syntax блокирует scanner OpenCode. Более широкий родительский
`allow` понижается до `ask`. Точный session scope Developer намеренно перекрывает
унаследованные project/user deny только для project edit/write, read-only
диагностики и перечисленных Runner-вызовов; защищённые пути запрещаются следующими
правилами. Scanner не передаёт hook тип соединяющего оператора, поэтому цепочка из
одних allowlisted read-only частей также безопасно разрешена; любая иная часть
возвращает весь запрос в `ask` или `deny`.

По умолчанию модели не изменены: `openai/gpt-5.6-sol` для Architect/Reviewer и
`opencode/deepseek-v4.1-flash-free` для Developer. Переменные
`AGENTIC_ARCHITECT_MODEL`, `AGENTIC_DEVELOPER_MODEL`, `AGENTIC_REVIEWER_MODEL`
принимают замену в формате `provider/model`.

## Оставшиеся ограничения

- Только последовательные задачи; нет параллельного scheduler или merge-сервера.
- Approval остаётся soft lock одного OS-пользователя.
- Auto-allow намеренно ограничен точной read-only диагностикой и привязанными к
  проекту вызовами Runner; другие безопасные варианты могут потребовать подтверждение.
- Wheel-проверка требует локально доступных build backend и зависимостей и ничего не
  скачивает (`--no-build-isolation`, `--no-deps`).
- Профили Go, JS/TS, Rust, Kotlin и C# не добавлены.
- Installer не меняет `opencode.jsonc`, не создаёт коммиты и не выполняет push,
  deploy или публикацию.

## Тесты v1.2

- Python unittest: legacy migration, последовательный A/B workflow, независимые
  approvals/receipts, review/accept/reopen audit, hashes, symlink-защита, TDD,
  изоляция wheel (включая ошибочную реализацию в artifact), installer и state separation.
- Node 24 mock plugin: `resume: false`, асинхронный Reviewer без premature completion,
  явные `/implement` и `/accept`, идемпотентность, `/task`, guards и shell permissions.
- Статическая проверка Python: `py_compile`.
- Реальный OpenCode 2.2.7 проверяется только в изолированных `HOME`/`XDG_*`.
