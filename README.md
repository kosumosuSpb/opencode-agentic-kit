# OpenCode Agentic Kit v0.8.0 (Beta)

OpenCode Agentic Kit добавляет в OpenCode управляемый процесс разработки Python-проектов:
Architect формирует спецификацию и план, Developer реализует их через TDD, Reviewer
проводит независимую проверку, а человек утверждает требования и принимает результат.

Kit связывает каждую задачу с утверждёнными документами, реальными запусками тестов и
Quality Gates. Он не делает commit, push или deploy и не принимает работу вместо человека.

## Как создавался проект

**OpenCode Agentic Kit разработан преимущественно с помощью искусственного интеллекта.**

- **ChatGPT — GPT-6 (High):** архитектура, требования, анализ решений, ревью,
  диагностика и координация разработки.
- **OpenAI Codex — GPT-5.6 Sol (High/Medium):** реализация, рефакторинг, тесты,
  исправление дефектов и технические проверки.

Через OpenCode при разработке и тестировании Kit отдельно использовались
**GPT-5.6 Sol** для ролей Architect и Reviewer и **DeepSeek V4.1 Flash Free** для
роли Developer.

Роль человека — постановка целей и ограничений, выбор технических решений,
организация экспериментов, практическая проверка и окончательная приёмка.

## Требования

- Linux, Git и Python 3.11 или новее.
- OpenCode 2.2.7 — версия, на которой проверялся этот Beta-релиз.
- Python-проект в Git-репозитории: OpenCode нужно запускать из его рабочего дерева.
- Оригинальный [`ub-mode`](https://github.com/unclebob/ub-mode). Скопируй его
  `SKILL.md` в `~/.config/opencode/skills/ub-mode/SKILL.md` или другой каталог,
  поддерживаемый [OpenCode Skills](https://opencode.ai/docs/skills/). Kit не
  устанавливает и не изменяет этот skill.
- Доступные в OpenCode модели для трёх ролей.

Для FAST Gate в окружении проекта нужны `ruff`, `mypy` и `pytest`. VERIFY и FULL
дополнительно используют `pytest-cov`/`coverage`, Crapper и Dryer, а FULL — Mutator.
Kit не устанавливает эти зависимости. Подробная схема размещения инструментов описана
в [документации по разработке и проверкам](docs/development.md#quality-gates).

## Быстрый старт

Сначала получи исходники Kit. Команды установки выполняются из каталога
`opencode-agentic-kit`, созданного `git clone`:

```bash
git clone https://github.com/kosumosuSpb/opencode-agentic-kit.git
cd opencode-agentic-kit
python3 install.py --dry-run
python3 install.py
```

Сам `install.py` работает локально, без `sudo` и дополнительных сетевых запросов. Он
добавляет plugin, роли и Runner в пользовательские каталоги OpenCode. После установки
перезапусти OpenCode.

Затем перейди из каталога исходников Kit в отдельный пользовательский Python-проект и
запусти OpenCode уже там:

```bash
cd /path/to/python-project
git status
opencode mini
```

Проект должен иметь Git root. Для нового каталога сначала выполни `git init`.

## Подготовка моделей

По умолчанию Kit выбирает:

- Architect: `openai/gpt-5.6-sol`;
- Developer: `opencode/deepseek-v4.1-flash-free`;
- Reviewer: `openai/gpt-5.6-sol`.

Каталог OpenCode и доступность моделей меняются, а credentials зависят от проекта.
Перед первой задачей подключи провайдеры и проверь точные ID:

```text
/connect
/models
```

Если default недоступен, задай любой доступный `provider/model` в окружении процесса,
который запускает OpenCode:

```bash
export AGENTIC_ARCHITECT_MODEL='provider/model'
export AGENTIC_DEVELOPER_MODEL='provider/model'
export AGENTIC_REVIEWER_MODEL='provider/model'
opencode mini
```

Plugin проверяет только формат `provider/model`; реальную доступность определяет
OpenCode. См. [Providers](https://opencode.ai/docs/providers) и
[Models](https://opencode.ai/docs/models/).

## Первая задача

В открытой сессии OpenCode выполни:

```text
/task new add-health-endpoint
/design Добавить health endpoint с тестами и обработкой ошибок
```

Прочитай созданные draft spec и plan. Если требования верны:

```text
/approve
/implement Реализовать утверждённый план
/review Проверить реализацию
```

Дождись независимого VERIFY-заключения Reviewer. Затем запусти финальную проверку:

```text
/finalize Подготовить итоговое заключение
```

`/finalize` не завершает задачу. Дождись результата, прочитай его и только при
согласии прими текущий финальный review:

```text
/accept
/accept <review-id>
```

Первая форма использует current final review. Вторая — совместимая явная форма для
того же текущего Review ID, показанного `/finalize`.

## Команды

| Команда | Назначение |
| --- | --- |
| `/task new <id>` | Создать и выбрать новую задачу. |
| `/task use <id>` | Переключиться на существующую задачу. |
| `/task status` | Показать реестр и активную задачу. |
| `/task reopen <id>` | Аудируемо открыть ранее завершённую задачу. |
| `/design <описание>` | Запустить Architect и создать draft spec/plan. |
| `/approve` | Утвердить draft и зафиксировать baseline. |
| `/approve replace` | Заменить прежнее утверждение с сохранением истории. |
| `/implement <контекст>` | Запустить Developer с approved plan, TDD и FAST Gate. |
| `/review <контекст>` | Выполнить VERIFY и независимое read-only ревью. |
| `/finalize <контекст>` | Выполнить FULL и получить финальное заключение. |
| `/accept` | Принять текущий финальный review после его чтения. |
| `/accept <review-id>` | Явно принять тот же текущий Review ID. |

ID задачи: 1–64 ASCII-буквы, цифры, `.`, `_` или `-`; первый символ — буква или
цифра. Если начать новый проект сразу с `/design`, Kit создаст `task-1`.

## TDD и Quality Gates

`/implement` передаёт Developer утверждённый план и требует реальные циклы
RED → GREEN → REFACTOR для feature- и regression-проверок. Команды тестов задаются
планом; Kit поддерживает pytest и unittest через разрешённый Python toolchain.

- **FAST** — Ruff check/format, Mypy и pytest; запускается во время реализации.
- **VERIFY** — FAST плюс coverage, Crapper, Dryer и при необходимости изолированная
  проверка wheel; запускается `/review`.
- **FULL** — VERIFY плюс Mutator; запускается `/finalize`.

Gate не считается успешным, если обязательный инструмент отсутствует или проверка
пропущена. Обычно запускать Gate вручную не требуется: slash-команды передают Runner
точные аргументы и связывают результат с текущей задачей. Команды ручной диагностики
приведены в [описании workflow](docs/workflow.md#ручная-диагностика).

## Обновление и rollback

Не обновляй Kit при работающих сессиях OpenCode. Из корня новой версии репозитория:

```bash
python3 install.py --dry-run
python3 install.py --upgrade
```

Upgrade разрешён только для неизменённой управляемой установки и сохраняет исходные
backup первой установки. Для возврата пользовательских файлов в исходное состояние:

```bash
python3 install.py --rollback
```

Rollback не предназначен для переключения между релизами: он восстанавливает состояние
до первой установки. При прерванной операции сначала также запускай `--rollback`.
Локальные изменения управляемых файлов не перезаписываются. Подробности и аварийные
сценарии: [Installer и recovery](docs/installer.md).

## Ограничения Beta

- Поддерживаются последовательные задачи; параллельного scheduler и автоматического
  merge конфликтов нет.
- Основной профиль Quality Gates рассчитан на Python-проекты.
- Approval — soft lock одного OS-пользователя, а не sandbox. Для недоверенного кода
  используй отдельного Linux-пользователя или CI runner.
- Пользователь отвечает за доступность моделей, `ub-mode` и инструментов Gates.
- Kit не выполняет commit, push, deploy или публикацию пакетов.
- Человек обязан прочитать спецификацию и review; Gate PASS не равен приёмке.

## Планы развития

### v0.9.0 — автоматизация и удобство использования

- Автоматическое обнаружение и подготовка необходимых инструментов разработки.
- Улучшенная поддержка выбора Python-интерпретатора и тестового окружения.
- Ранняя проверка полноты acceptance criteria до начала реализации.
- Оптимизация контекста Reviewer и снижение расхода токенов.
- Улучшение управления задачами, состояниями и историей workflow.

### v1.0.0 — первый стабильный релиз

Стабилизация пользовательских интерфейсов и совместимости после практической
эксплуатации Kit в нескольких реальных проектах.

Планы предварительные и могут изменяться. Подробнее: [Roadmap проекта](docs/ROADMAP.md).

## Подробная документация

- [Installer, upgrade, rollback и recovery](docs/installer.md)
- [Задачи, approvals, evidence, reviews и state](docs/workflow.md)
- [Permissions, shell policy и безопасность](docs/security.md)
- [Разработка и тестирование самого Kit](docs/development.md)

## Лицензия

OpenCode Agentic Kit распространяется по лицензии [Apache License 2.0](LICENSE).
