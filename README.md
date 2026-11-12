# Agentic Kit v1.2-preview.2 — стабилизация перед приёмкой

Переносимая **промежуточная версия**, а не опубликованный выпуск v1.2. Развивается в каноническом Git-репозитории `opencode-agentic-kit` на основе проверенной v1.1. Текущая работа стабилизирует существующую сборку; она не создаёт новую preview-версию и ничего не публикует.

## Требования

- Linux, Git и Python 3.11 или новее.
- OpenCode 2.2.7 для реальной интеграционной проверки.
- Оригинальный навык `ub-mode` в пользовательской установке агента.
- Node.js 24 только для JavaScript-теста. `npm` и глобальный пакет `typescript` не требуются.

## Что изменено

- Глобальный плагин `agentic.workflow.preview`: все пять команд `/design`, `/approve`, `/implement`, `/review`, `/finalize`.
- Проект извлекается из **данных конкретной сессии** OpenCode, потом нормализуется через `git rev-parse --show-toplevel`; `ctx.location` не используется как подмена каталога сессии. Если сессия не сообщает абсолютный каталог ни в `directory`, ни в `location.directory`, команда блокируется.
- Плагин запускает Runner асинхронно, `execFile` без shell, без жёсткой ссылки на лабораторию.
- Явное переключение моделей сохранено; `AGENTIC_ARCHITECT_MODEL`, `AGENTIC_DEVELOPER_MODEL`, `AGENTIC_REVIEWER_MODEL` могут заменить дефолты в формате `provider/model`.
- В Runner удалены исключения для лаборатории и отдельного имени задачи. Вместо обязательного `src/` разрешены Python-модули в корне. Для проектов без `uv.lock` используется `.venv/bin/python` или Python, запустивший Runner; без создания lockfile.
- Отличаются `FAIL` (проверка обнаружила дефект, exit 1) и `BLOCKED` (проверка не могла выполняться достоверно, exit 2).
- Поддержаны независимые project-specific baseline/receipt в user state; сохранена проверка Git fingerprint и неизменности утверждённых документов.
- Обновлены устанавливаемые профили: Architect пишет только в `.agentic/drafts/**`, Reviewer остаётся read-only.
- Установщик запускается из канонического дерева исходников: без sudo, с первоначальным backup, dry-run по умолчанию, явным `--upgrade`, отказом от отката или обновления при локальных правках установленных файлов. `--rollback` после обновления восстанавливает состояние до первой preview-установки.
- Approval защищён от частично созданного baseline при ошибке записи и от символьных ссылок в целевых путях.
- `/approve` отправляет синтетическое сообщение с `resume: false`: утверждение фиксируется без самостоятельного продолжения Build/Developer; только явный `/implement` запускает Developer.
- Правила ролей автоматически разрешают встроенное чтение и узкую Git-инспекцию, оставляют произвольный shell и запись Developer под подтверждением. Permission hook требует отдельного решения для `git commit` и запрещает `git push`/package publish даже при позднем разрешающем project config.
- Runner хранит TDD evidence и receipts во внешнем project-specific state store. Verify/Full дополнительно собирают обычную wheel, ставят её в изолированный каталог и запускают тесты вне корня исходников.

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

**Важно:** имя установленного каталога `opencode-agentic/preview/` и ID `agentic.workflow.preview` оставлены временно для совместимости с текущей версией. Они **не являются каталогом исходников**. Перевод на полноценную release-layout установку с версиями и атомарным переключением будет отдельным этапом v1.2; здесь его не имитируем.

В отдельном терминале открой новый проект и проверь наличие пяти команд:

```bash
cd ~/dev/own/ru-web-fetcher
opencode mini
```

Для проверки разрешения проекта и корректного блокирования до approval:

```bash
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" check-approval
```

Ожидаемо: `BLOCKED: NO_APPROVAL` (если ещё не утверждали draft в этом репозитории). Это не ошибка установки, а важная защита.

## Модели и команды

По умолчанию используются `openai/gpt-5.6-sol` для Architect и Reviewer и `opencode/deepseek-v4.1-flash-free` для Developer. Их можно заменить перед запуском OpenCode:

```bash
export AGENTIC_ARCHITECT_MODEL='provider/model'
export AGENTIC_DEVELOPER_MODEL='provider/model'
export AGENTIC_REVIEWER_MODEL='provider/model'
```

Значение обязательно имеет формат `provider/model`. Основной цикл: `/design` создаёт черновики, человек проверяет их и запускает `/approve`, `/implement` выполняет утверждённый план, `/review` проводит независимую проверку, `/finalize` запускает полный gate и готовит заключение. Утверждение является процедурным soft lock, а окончательное решение всегда остаётся за человеком.

Для проверяемого TDD-журнала Developer запускает каждый этап через Runner (произвольная команда остаётся под обычным shell-подтверждением OpenCode):

```bash
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" tdd --cycle normalize-empty --stage red \
  --expect "новый тест отклоняет пустое имя" -- python -m pytest -q tests/test_name.py
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" tdd --cycle normalize-empty --stage green -- python -m pytest -q tests/test_name.py
python3 ~/.local/share/opencode-agentic/preview/runner/agentic_gate.py \
  --project "$PWD" evidence
```

Runner принимает здесь только прямые команды pytest/unittest (включая `python -m` и `uv run --locked`) и записывает этап лишь после реального запуска: для RED обязательна заранее указанная ожидаемая причина и код 1, для GREEN/REFACTOR — код 0. `evidence` — поддерживаемый интерфейс просмотра внешних receipts и TDD-журнала; искать эти файлы в `.agentic/` не нужно. Verify/Full receipts содержат версии/команды/коды возврата и признак wheel-проверки. PASS receipt подтверждает конкретный fingerprint и baseline, но не заменяет доказательство каждого acceptance criterion.

`/design` можно проверить на небольшой фиктивной задаче. **Не запускай `/approve` для основной MCP-разработки, пока не рассмотрены draft и acceptance criteria.**

Перезапусти OpenCode после установки или отката. Локальные файлы лаборатории `opencode-agentic-lab/.opencode/**` не изменяются, однако **глобальный preview-плагин и лабораторный проектный v1.1-плагин могут одновременно регистрировать одноимённые команды**. Не используйте лабораторию в режиме preview до проверки этой коллизии; для чистого возврата выполни `--rollback`.

## Что пока НЕ реализовано и НЕ проверено

- Это всё ещё **single-task baseline** на проект, как в v1.1. Новая задача после завершения старой без миграции state ещё не поддерживается.
- Approval процедурный (`--human-confirm`), **не является доказательством человеческого происхождения** команды или OS-изоляцией.
- Не реализованы профили JS/TS, Rust, Kotlin, C#.
- Реальный **OpenCode 2.2.7** без модельных вызовов успешно загрузил plugin и три agent frontmatter в изолированном профиле. Исполнение `/approve`/`/implement` покрыто mock API; если реальная сессия не возвращает directory, будет явный `PROJECT_UNRESOLVED`.
- Родительский `opencode.jsonc` может дописывать правила после agent frontmatter. Permission hook понижает любые поздние auto-allow для произвольного shell до `ask` и запрещает распознанные push/publish, но не ослабляет внешний `deny` (в текущем родительском config Architect по-прежнему не получает shell-доступ).
- Verify/Full для плоских проектов запускают Crapper/Dryer/Mutator с перечисленными source paths. Их совместимость с такими аргументами ещё не подтверждена на твоей установке. Wheel-проверка требует локально доступных build backend и зависимостей и намеренно не скачивает зависимости через pip (`--no-build-isolation`, `--no-deps`). **Никаких заявлений о FULL PASS до реального запуска.**
- Подлинный `ub-mode` остаётся на твоей текущей установке (`~/.agents/skills/ub-mode`), не копируется и не меняется.
- Installer не меняет `opencode.jsonc`, не создаёт GitHub-репозиторий, не запускает push или deploy.

## Тесты после исправлений preview.2

- Python unittest: 24 зелёных теста (permissions, Git root, approval, fingerprint, receipts, TDD evidence, pip/uv wheel, Python flat/src, разделение состояния проектов, установщик и откат).
- Node 24 mock plugin test без npm-зависимостей: `/approve` не возобновляет модель, явный `/implement` запускает Developer, сохранены guards, пять команд, выбор модели и несколько репозиториев.
- Статическая проверка синтаксиса Python: `py_compile`.
- Ранее на распакованных исходниках `ru-web-fetcher` обнаружены четыре корневых Python-модуля; реальное повторное тестирование проекта пока не проводилось.

Это **тесты прототипа** и безмодельная проверка загрузки OpenCode 2.2.7, а не подтверждение полного интерактивного цикла или полноценного quality gate на MCP-проекте.
