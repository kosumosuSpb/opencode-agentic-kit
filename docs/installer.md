# Installer, upgrade, rollback и recovery

Этот документ описывает техническую семантику `install.py`. Пользовательский сценарий
находится в [README](../README.md#быстрый-старт).

## Область установки

Установщик работает локально, не использует сеть или `sudo` и учитывает XDG-переменные:

- config: `${XDG_CONFIG_HOME:-$HOME/.config}/opencode`;
- data: `${XDG_DATA_HOME:-$HOME/.local/share}/opencode-agentic/preview`;
- state: `${XDG_STATE_HOME:-$HOME/.local/state}/opencode-agentic/preview-install`.

Он устанавливает plugin `agentic-preview`, роли Architect/Developer/Reviewer и Python
Runner, а также регистрирует TUI plugin в `cli.json`. `opencode.jsonc` не изменяется.

Имена `preview`, `agentic-preview` и `agentic.workflow.preview` сохранены для
совместимости с прежними установками. Они не обозначают отдельный публичный preview-
релиз. Публичная версия продукта — `0.8.0`; внутреннее значение version в manifest
остаётся `1.2` исключительно как идентификатор совместимости установщика.

## Режимы

```bash
python3 install.py --dry-run
python3 install.py
python3 install.py --install
python3 install.py --upgrade
python3 install.py --rollback
```

- Запуск без аргументов и `--install` — одинаковая новая установка.
- `--dry-run` показывает план и не создаёт config/data/state.
- Повтор той же установки возвращает `ALREADY_INSTALLED`.
- Если canonical sources изменились, обычная установка блокируется с
  `UPGRADE_REQUIRED`; требуется осознанный `--upgrade`.
- `--upgrade` требует существующий manifest и неизменённые managed targets.
- `--rollback` восстанавливает состояние до первой установки, а не предыдущий релиз.

Не запускай install/upgrade/rollback параллельно или при активных сессиях OpenCode.

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

## Транзакции и прерывания

Операции сериализованы через постоянный `preview-install/installer.lock` и `flock`.
Занятый lock возвращает `INSTALLER_BUSY`; сам файл lock после операции не удаляется.

До первого изменения `cli.json` или release target создаются
`preview-install/transaction.json` и recovery backup. После жёсткого прерывания новый
install/upgrade возвращает `INCOMPLETE_TRANSACTION`: сначала выполни `--rollback`.

- recovery незавершённого install возвращает пользовательское состояние до установки;
- recovery незавершённого upgrade возвращает предыдущую установленную версию;
- после recovery upgrade обычный rollback всё ещё может вернуть состояние до первой
  установки;
- изменения targets, сделанные после сбоя, не перезаписываются и дают `LOCAL_CHANGE`.

Rollback ведёт прогресс в manifest и может безопасно продолжиться после остановки между
записью target и фиксацией прогресса. Перед окончательным удалением manifest повторно
проверяется восстановленное состояние.

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

## Операционная памятка

- `NOT_INSTALLED`: для `--upgrade` нет manifest либо rollback уже завершён.
- `UPGRADE_REQUIRED`: sources отличаются; используй `--upgrade` после проверки diff.
- `LOCAL_CHANGE`: managed target или recovery state изменён пользователем.
- `BACKUP_CHANGED`/`BACKUP_MISSING`: безопасный rollback невозможен.
- `UNVERIFIED_BACKUP`: старому backup нельзя автоматически доверять.
- `INCOMPLETE_TRANSACTION`: сначала возобнови recovery через `--rollback`.
- `INSTALLER_BUSY`: дождись завершения другой операции.
