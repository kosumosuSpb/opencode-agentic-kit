# Разработка и тестирование Kit

Этот документ предназначен для contributors самого OpenCode Agentic Kit. Пользователю
своего Python-проекта достаточно [README](../README.md).

## Проверка исходного репозитория

Все команды выполняются из корня canonical checkout, не из установленной копии:

```bash
python3 -m unittest discover -s tests -v
node tests/test_plugin.cjs
python3 install.py --dry-run
```

Для plugin suite нужен Node.js 24. Если используется nvm, точная команда проекта:

```bash
nvm exec 24 node tests/test_plugin.cjs
```

Python suite и Node plugin suite независимы; Runner не заменяет JavaScript-проверку.
Для mock plugin suite не требуются `npm` или глобальный пакет `typescript`.

Installer-тесты используют только временные `HOME`/`XDG_*`. Никогда не направляй их на
реальную глобальную установку OpenCode. Изменения installer/upgrade/rollback требуют
тестов прерывания и idempotent recovery.

## Quality Gates

Runner выбирает backend так:

1. при наличии `uv.lock` — `uv run --locked`;
2. иначе `.venv/bin/python`, если он существует;
3. иначе Python, которым запущен Runner.

FAST требует доступные в выбранном backend модули `ruff`, `mypy`, `pytest` и запускает:

- `ruff check` для production и test Python files;
- `ruff format --check` для тех же файлов;
- Mypy для production sources;
- `pytest -q tests`.

VERIFY дополнительно требует `pytest_cov` и `coverage`, создаёт актуальный LCOV, затем
запускает Crapper с порогом CRAP ≤ 10 и Dryer с порогом similarity 0.8. FULL добавляет
Mutator с максимум двумя workers.

Внешние инструменты ищутся по layout:

```text
${AGENTIC_TOOLS_DIR:-$HOME/.local/share/opencode-agentic/tools}/crapper/crapper
${AGENTIC_TOOLS_DIR:-$HOME/.local/share/opencode-agentic/tools}/dryer/dryer
${AGENTIC_TOOLS_DIR:-$HOME/.local/share/opencode-agentic/tools}/mutator/mutator
```

Kit не устанавливает их автоматически. Исходные проекты инструментов указаны в
[оригинальном ub-mode](https://github.com/unclebob/ub-mode#companion-tool-repositories).
Сообщение `MISSING_QUALITY_TOOL` — блокер Gate, а не разрешение пропустить проверку.

Для прямого запуска после создания и approval активной задачи:

```bash
python3 runner/agentic_gate.py --project "$PWD" gate fast
python3 runner/agentic_gate.py --project "$PWD" gate verify
python3 runner/agentic_gate.py --project "$PWD" gate full
```

Используй targeted tests во время разработки, FAST после связного блока, VERIFY перед
review и FULL только перед release/finalization. Не запускай дорогой FULL после каждого
малого изменения.

## Coverage и wheel

Coverage строится для всех обнаруженных production Python sources и сохраняется в
`target/coverage/python/lcov.info`. Старый отчёт не выдаётся за актуальный: новый файл
публикуется только после успешной проверки состава sources.

Если проект содержит поддерживаемые packaging metadata, VERIFY/FULL:

1. собирает обычный wheel без сети (`--no-build-isolation`, `--no-deps`);
2. устанавливает artifact в отдельный временный target;
3. импортирует обнаруженный top-level API через изолированный Python без source root;
4. проверяет, что `__file__`/`__path__` находятся внутри target;
5. копирует pytest и его runtime dependencies в отдельный временный toolsite;
6. запускает копию project-owned тестов с target первым в `sys.path`;
7. сохраняет hash wheel и команды в receipt.

Для script-only проекта wheel получает `not_applicable`, а не ложный PASS. Недоступный
build backend или dependency остаётся явным блокером; Gate ничего не скачивает ради
зелёного статуса.

## Документационные и release-проверки

Перед review документационного патча проверь относительные Markdown-ссылки,
противоречивые версии и `git diff --check`. Перед релизом дополнительно нужны штатные
VERIFY/FULL и реальная изолированная установка, но их не следует повторять для каждого
малого изменения текста.

Python regression suite покрывает task lifecycle, approvals/receipts/reviews, TDD,
installer recovery, symlink safety, coverage и wheel isolation. Node mock suite отдельно
проверяет команды plugin, guards, permissions, idempotency и асинхронный Reviewer.
Реальную интеграцию OpenCode проверяй только с изолированными `HOME`/`XDG_*`.

Не выполняй commit, tag, push, deploy или publication без отдельного человеческого
решения.
