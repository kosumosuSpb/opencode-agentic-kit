# Задачи, approvals, evidence, reviews и state

README показывает [первую задачу](../README.md#первая-задача). Здесь описаны границы
данных и переходы workflow.

## Задачи и layout

Реестр задач хранится во внешнем project-specific state. Новая задача получает ID из
1–64 ASCII-букв, цифр, `.`, `_` и `-`; первый символ — буква или цифра.

```text
.agentic/tasks/<id>/drafts/spec.md
.agentic/tasks/<id>/drafts/plan.md
specs/tasks/<id>/approved.md
plans/tasks/<id>/approved.md
$AGENTIC_STATE_DIR/projects/<project-id>/tasks/<id>/
```

По умолчанию внешний state находится в
`$HOME/.local/state/opencode-agentic/projects/<project-id>/`. Переменная
`AGENTIC_STATE_DIR` заменяет корень `$HOME/.local/state/opencode-agentic`; в отличие от
installer state, workflow state не читает `XDG_STATE_HOME`. Project ID вычисляется из
абсолютного Git top-level, поэтому разные checkout/worktree не разделяют state случайно.

Если `tasks.json` отсутствует, legacy layout создаётся только при обнаружении прежних
workflow-артефактов: внешнего `baseline` либо `.agentic/drafts/{spec,plan}.md`,
`specs/approved.md` или `plans/approved.md`. Такой проект получает задачу `legacy` без
перемещения файлов. Номер версии проекта сам по себе не определяет legacy.

Если прежних артефактов нет, `/design` создаёт `task-1`; `/task new <id>` создаёт
задачу с выбранным ID. Одновременно активна одна задача. `/task new`, `/task use`,
`/task status` и `/task reopen` показывают результат через TUI toast с `resume: false`,
не будят модель и сами по себе не расходуют токены модели.

## Design и approval

Architect может изменять только draft spec и plan активной задачи. `/approve`:

1. проверяет, что документы пригодны для утверждения;
2. создаёт approved-копии;
3. сохраняет task-specific baseline и hashes;
4. не запускает Developer автоматически.

Draft должен иметь единственную строку статуса. При публикации Runner заменяет её на
`APPROVED — утверждено человеком` и блокирует противоречивые остаточные `DRAFT`,
«требует утверждения» и «не является утверждённым» в основном тексте.

Approved spec/plan неизменяемы до явного `/approve replace`. Замена сначала проверяет
старые hashes, архивирует прежний baseline, approved documents, TDD и receipts в
`approval-history/`, затем публикует новый baseline. Старые evidence не становятся
доказательством новых требований.

Approval и baseline принадлежат конкретной задаче: утверждение одной задачи не даёт
Developer доступ к другой.

Approval — soft lock: другой процесс того же OS-пользователя технически может вызвать
Runner напрямую. Для недоверенного кода нужна отдельная OS-учётная запись или CI runner.

## TDD evidence и acceptance checks

`/implement` получает от Runner active task, approved paths, проверенный approval,
компактное состояние TDD/receipts/acceptance checks и выбранный Python toolchain.
Для первичной реализации Developer фиксирует реальные RED/GREEN/REFACTOR для
`feature` и `regression`. RED принимается только при ожидаемой причине и exit code 1;
ошибка collection/usage не считается RED. GREEN и REFACTOR требуют exit code 0.
Команды feature/regression определяет approved plan; `python.test_command` из
implement-context предназначен для Quality Gates и не заменяет TDD-команды плана.

Исторический TDD-журнал доказывает порядок разработки, но не заменяет актуальные тесты
после изменения fingerprint. Если TDD для текущего approval уже завершён, исправление
замечаний линтера или Reviewer не требует искусственного RED: Developer сохраняет журнал,
повторяет нужные regression/acceptance checks и Gate. Для acceptance criterion, который
требует конкретной команды, Runner поддерживает отдельный
`check --criterion <id> -- <command>`. Остальные функциональные критерии могут быть
подтверждены актуальным Gate и независимым review.

Компактный `workflow_state` различает наличие хотя бы одного завершённого цикла и
завершённость всех записанных циклов. Developer сверяет каждый цикл с approved plan:
новое поведение без собственного цикла требует TDD, а незавершённый цикл нельзя
пропустить из-за завершённости другого. TDD относится к текущей реализации только при
совпадении baseline с approval; legacy-журнал без baseline считается текущим лишь пока
не существует история замен approval.

## Quality receipts

Receipt связывает результат Gate с:

- task ID;
- hashes approved spec/plan;
- fingerprint tracked и untracked project files;
- фактически выполненными командами;
- результатом wheel-проверки, когда она применима.

Изменение кода, approval или active task делает receipt stale. Receipt хранится во
внешнем state и не обязан находиться в рабочем дереве. Успех unit-тестов сам по себе не
заменяет свежий требуемый Gate.

## Review, finalize и acceptance

`/review` выполняет VERIFY, создаёт Review ID, сохраняет binding и запускает независимый
read-only Reviewer. Reviewer обязан записать один verdict: `pass`, `reject` или
`exception`, вместе с конкретными findings. Отправка prompt или Gate PASS сами по себе
не являются review PASS. `major` и `process` findings несовместимы с PASS; process
exception хранится отдельно и блокирует acceptance.

`/finalize` выполняет FULL. Если task/approval/fingerprint прежнего `/review` точно
совпадают, Reviewer переиспользует независимое чтение и проверяет новые FULL/mutation
evidence и carried findings. Иначе выполняется полноценный fallback review. Pending или
уже passed текущий finalize-review не запускается повторно.

`/finalize` не завершает задачу. После чтения PASS-заключения человек использует:

```text
/accept
```

Команда без аргумента берёт только current final review активной задачи. Явная форма
`/accept <review-id>` поддерживается для того же текущего Review ID. Rejected,
exception, pending, stale или superseded review не принимаются. Повторный `/accept`
идемпотентен. Прямая Runner-команда `complete` отключена.

## Reopen и история

`/task reopen <id>` — явное человеческое действие. Оно сохраняет прежние completion и
acceptance в `status_history`, архивирует текущие TDD/receipts/reviews, очищает только
текущий completion status и требует новый `/review` или `/finalize` с последующим
`/accept`. Reopen provenance и архивы отображаются в `task_audit`, но исторические
evidence не выдаются за текущие. Не редактируй `tasks.json`, pointers или state-файлы
вручную.

## Ручная диагностика

Из Git root проекта можно прочитать task state и собранные evidence без запуска модели:

```bash
AGENTIC_RUNNER="${XDG_DATA_HOME:-$HOME/.local/share}/opencode-agentic/runner/agentic_gate.py"
python3 "$AGENTIC_RUNNER" --project "$PWD" task-status
python3 "$AGENTIC_RUNNER" --project "$PWD" evidence
```

Путь Runner относится к installer data и поэтому учитывает `XDG_DATA_HOME`. Остальные
Runner-команды обычно вызывает plugin: он передаёт точный Git root и допустимые argv.
