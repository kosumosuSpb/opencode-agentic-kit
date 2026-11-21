# Installer, upgrade, rollback и recovery

Этот документ описывает техническую семантику `install.py`. Пользовательский сценарий
находится в [README](../README.md#быстрый-старт).

## Область установки

Установщик работает локально, не использует сеть или `sudo` и учитывает XDG-переменные:

- config plugin: `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/agentic-kit`;
- data: `${XDG_DATA_HOME:-$HOME/.local/share}/opencode-agentic/runner`;
- state: `${XDG_STATE_HOME:-$HOME/.local/state}/opencode-agentic/install`.

Он устанавливает package `opencode-agentic-kit`, роли Architect/Developer/Reviewer и
Python Runner. OpenCode автоматически обнаруживает package в глобальном каталоге
`plugins/` и загружает его server- и TUI-entrypoints. Installer не регистрирует тот же
package повторно в `cli.json`; существующие дублирующие регистрации Kit удаляются
транзакционно. `opencode.jsonc` не изменяется.

Внутренние workflow ID `agentic.workflow.preview` и
`agentic.workflow.preview.tui` сохранены, чтобы не менять идентичность workflow и
существующее состояние задач. Публичная версия продукта — `0.8.2`; manifest содержит
`product_version: 0.8.2`, а внутреннее поле `version: 1.2` остаётся идентификатором
совместимости формата установщика.

Manifest установки v0.8.0 находится в legacy state
`${XDG_STATE_HOME:-$HOME/.local/state}/opencode-agentic/preview-install/manifest.json`.
Если он существует, dry-run/install/upgrade v0.8.2 завершается с `LEGACY_INSTALLATION` до
создания нового Kit. Автоматическая миграция v0.8.0 → v0.8.2 не реализована. Текущий installer может
полностью удалить legacy-установку через `--uninstall`; после этого возможна чистая
установка v0.8.2.

Для существующей установки v0.8.1 с canonical manifest используется тот же layout:
после получения исходников v0.8.2 и завершения активных сессий OpenCode запусти
`python3 install.py --dry-run`, затем `python3 install.py --upgrade`.
Внутренний формат manifest `version: 1.2` при этом не изменяется.
Не используй `--install` вместо явного `--upgrade`, если изменились managed files.

## Режимы

```bash
python3 install.py --dry-run
python3 install.py
python3 install.py --install
python3 install.py --upgrade
python3 install.py --rollback
python3 install.py --uninstall
python3 install.py --uninstall --discard-backups
```

- Запуск без аргументов и `--install` — одинаковая новая установка.
- `--dry-run` показывает план и не создаёт config/data/state.
- Повтор той же установки возвращает `ALREADY_INSTALLED`.
- Если canonical sources изменились, обычная установка блокируется с
  `UPGRADE_REQUIRED`; требуется осознанный `--upgrade`.
- `--upgrade` требует существующий manifest и неизменённые managed targets.
- После успешного install/upgrade сообщение рекомендует
  `python3 install.py --uninstall` как штатное полное удаление.
- `--rollback` выполняет recovery незавершённого install/upgrade либо восстанавливает
  состояние до первой установки с сохранением backup; это не команда удаления.
- `--uninstall` восстанавливает исходные файлы и полностью удаляет проверенные
  Plugin, Runner, глобальные агенты, регистрации и служебный state v0.8.0 или v0.8.1+.
- `--rollback` сохраняет backup и прежнюю семантику; `--uninstall` является отдельной
  командой и после успешного восстановления удаляет активные проверенные backup.
- `--discard-backups` допустим только вместе с `--uninstall` и требует осознанного
  отказа от восстановления исходных файлов, подлинность которых нельзя проверить.

Не запускай install/upgrade/rollback/uninstall параллельно или при активных сессиях
OpenCode.

## Manifest и защита изменений

Manifest хранит список managed-файлов, их установленные SHA-256, сведения о наличии
исходного файла, backup и trusted hash исходного содержимого. Более старый manifest
может содержать только подмножество текущих targets: upgrade добавляет новые targets,
но сохраняет исходные backup и `existed` flags.

Перед upgrade или rollback Kit сравнивает managed-файлы с manifest. Неизвестное
изменение даёт `LOCAL_CHANGE`; установщик не перезаписывает пользовательскую правку.
Не редактируй manifest или backup вручную.

Новые установки записывают `backup_hash`. До rollback все backup проверяются; изменение
даёт `BACKUP_CHANGED` до записи в targets. Старый manifest без trusted original hash не
позволяет автоматически восстановить существовавший файл: операция завершается с
`UNVERIFIED_BACKUP`, сохраняя targets, manifest и backup для ручной проверки. Если в
legacy manifest все targets изначально отсутствовали и backup не создавались, Kit может
безопасно удалить созданные им файлы.

## Принудительный отказ от backup

Обычный `python3 install.py --uninstall` — предпочтительный и безопасный режим. Если он
завершился с `UNVERIFIED_BACKUP`, сначала вручную проверь сохранённые backup и реши,
нужно ли восстановить прежнее содержимое. Отсутствие trusted hash не означает, что
backup повреждён: installer лишь не может доказать его подлинность автоматически.

Команда `python3 install.py --uninstall --discard-backups` нужна только для удаления
legacy-установки, когда пользователь сознательно отказывается от восстановления всех
исходных managed-файлов без trusted hash. Для каждого такого файла команда:

1. удаляет установленный managed target вместо восстановления прежнего содержимого;
2. завершает uninstall и удаляет активный backup, указанный manifest.

Это необратимо средствами Kit: прежнее содержимое непроверяемого backup будет потеряно.
Проверяемые по hash исходные файлы восстанавливаются как при обычном uninstall.
Для непроверяемого `cli.json` сохраняется его текущее содержимое, но из него удаляются
только регистрации Kit, поэтому посторонние текущие настройки остаются. Команда также
удаляет проверенные компоненты Kit и активный manifest; она не удаляет проектные
`.agentic`, сохранённые задачи, чужие plugins, `backup-archives/`, recovery-поколения и
неизвестные данные в `backups/`.

Если uninstall был прерван после фиксации режима, продолжай его с тем же набором
аргументов. Смена режима даёт `UNINSTALL_MODE_CHANGED`, чтобы случайно не изменить
решение о восстановлении на середине операции.

## Транзакции и прерывания

Операции сериализованы через постоянный `install/installer.lock` и `flock`.
Занятый lock возвращает `INSTALLER_BUSY`; сам файл lock после операции не удаляется.

До первого изменения `cli.json` или release target создаются
`install/transaction.json` и recovery backup. После жёсткого прерывания новый
install/upgrade возвращает `INCOMPLETE_TRANSACTION`: сначала выполни `--rollback`.

- recovery незавершённого install возвращает пользовательское состояние до установки;
- recovery незавершённого upgrade возвращает предыдущую установленную версию;
- после recovery upgrade обычный rollback всё ещё может вернуть состояние до первой
  установки;
- изменения targets, сделанные после сбоя, не перезаписываются и дают `LOCAL_CHANGE`.

Rollback ведёт прогресс в manifest и может безопасно продолжиться после остановки между
записью target и фиксацией прогресса. Перед окончательным удалением manifest повторно
проверяется восстановленное состояние.

Uninstall использует тот же resumable restore, затем фиксирует в manifest отдельную
фазу `cleanup` с hashes фактически восстановленных файлов. Поэтому остановка до очистки,
после удаления части backup или перед удалением manifest безопасно продолжается повторным
`--uninstall`. Команда одновременно удерживает lock обоих известных layouts; наличие
обоих state/manifest считается неоднозначным и даёт `MULTIPLE_INSTALLATIONS` без записи
в targets.

## Файловая безопасность

Установщик не следует по symbolic links ни в одном существующем компоненте managed-
пути. Это относится к XDG-каталогам, targets, backup, manifest, recovery и lock.
Symlink или не-каталог в цепочке пути даёт `UNSAFE_PATH`; отсутствующие обычные каталоги
создаются относительно уже открытого родительского descriptor.

Операции записи используют временный файл, `fsync`, атомарный replace и синхронизацию
родительского каталога. Backup создаются без замены уже существующего файла.

## Сохранение backup

Успешный rollback намеренно не удаляет исходные backup. При следующей новой установке,
когда активного manifest уже нет, непустой `backups/` атомарно переносится в
`backup-archives/backup-<unique-id>/`. Коллизия имени никогда не заменяет архив:
выбирается новое имя. При активном manifest или незавершённом rollback архивирование
запрещено. Архивы удаляются только вручную после проверки.

Успешный uninstall удаляет только backup, перечисленные и проверенные активным manifest.
Непустые `backup-archives/`, recovery-поколения и неизвестные данные в `backups/`
сохраняются с сообщением `RETAINED_BACKUPS`; автоматически доказать безопасность их
удаления нельзя. Постоянный `installer.lock` может остаться как безопасная точка
межпроцессной синхронизации.

## Операционная памятка

- `NOT_INSTALLED`: для `--upgrade` нет manifest либо rollback уже завершён.
- `LEGACY_INSTALLATION`: найден manifest v0.8.0; автоматической миграции нет.
- `UPGRADE_REQUIRED`: sources отличаются; используй `--upgrade` после проверки diff.
- `LOCAL_CHANGE`: managed target или recovery state изменён пользователем.
- `BACKUP_CHANGED`/`BACKUP_MISSING`: безопасный rollback невозможен.
- `UNVERIFIED_BACKUP`: старому backup нельзя автоматически доверять.
- `INCOMPLETE_TRANSACTION`: сначала возобнови recovery через `--rollback`.
- `INSTALLER_BUSY`: дождись завершения другой операции.
- `MULTIPLE_INSTALLATIONS`: одновременно найдены legacy state v0.8.0 и canonical state v0.8.1+; требуется
  ручная проверка перед удалением.
- `RETAINED_BACKUPS`: архив сохранён, потому что его безопасное удаление не доказано.
