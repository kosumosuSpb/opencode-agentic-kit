"""Opt-in OpenCode Agentic Kit installer from this source tree. No network or sudo."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent
# Persisted installer compatibility identifier. Keep stable across v0.8.1+ manifests.
INSTALLER_COMPATIBILITY_VERSION = '1.2'
PRODUCT_VERSION = '0.8.2'
LEGACY_TUI_PLUGIN_SPEC = './plugins/agentic-preview/tui.ts'
TUI_PLUGIN_SPEC = './plugins/agentic-kit'


def _xdg_home(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else fallback


def directories():
    home = Path.home()
    config = _xdg_home('XDG_CONFIG_HOME', home / '.config')
    data = _xdg_home('XDG_DATA_HOME', home / '.local/share')
    state = _xdg_home('XDG_STATE_HOME', home / '.local/state')
    return (
        config / 'opencode',
        data / 'opencode-agentic',
        state / 'opencode-agentic/install',
    )


def legacy_manifest() -> Path:
    home = Path.home()
    state = _xdg_home('XDG_STATE_HOME', home / '.local/state')
    return state / 'opencode-agentic/preview-install/manifest.json'


def legacy_directories() -> tuple[Path, Path, Path]:
    config, data, state = directories()
    return config, data / 'preview', state.parent / 'preview-install'


def require_no_legacy_installation() -> None:
    legacy = legacy_manifest()
    if managed_exists(legacy):
        raise RuntimeError(
            f'LEGACY_INSTALLATION: found v0.8.0 manifest at {legacy}; '
            'migration to v0.8.2 is not implemented. '
            'Run python3 install.py --uninstall before installing v0.8.2.'
        )


def targets():
    config, data, _ = directories()
    return {
        config / 'plugins/agentic-kit/index.ts': ROOT / 'plugin/index.ts',
        config / 'plugins/agentic-kit/tui.ts': ROOT / 'plugin/tui.ts',
        config / 'plugins/agentic-kit/package.json': ROOT / 'plugin/package.json',
        config / 'agents/architect.md': ROOT / 'agents/architect.md',
        config / 'agents/developer.md': ROOT / 'agents/developer.md',
        config / 'agents/reviewer.md': ROOT / 'agents/reviewer.md',
        data / 'runner/__init__.py': ROOT / 'runner/__init__.py',
        data / 'runner/agentic_gate.py': ROOT / 'runner/agentic_gate.py',
        data / 'runner/agentic/__init__.py': ROOT / 'runner/agentic/__init__.py',
        data / 'runner/agentic/cli.py': ROOT / 'runner/agentic/cli.py',
        data / 'runner/agentic/commandline.py': ROOT / 'runner/agentic/commandline.py',
        data / 'runner/agentic/approval.py': ROOT / 'runner/agentic/approval.py',
        data / 'runner/agentic/acceptance_criteria.py': ROOT / 'runner/agentic/acceptance_criteria.py',
        data / 'runner/agentic/acceptance_checks.py': ROOT / 'runner/agentic/acceptance_checks.py',
        data / 'runner/agentic/coverage.py': ROOT / 'runner/agentic/coverage.py',
        data / 'runner/agentic/evidence.py': ROOT / 'runner/agentic/evidence.py',
        data / 'runner/agentic/errors.py': ROOT / 'runner/agentic/errors.py',
        data / 'runner/agentic/execution.py': ROOT / 'runner/agentic/execution.py',
        data / 'runner/agentic/project.py': ROOT / 'runner/agentic/project.py',
        data / 'runner/agentic/quality.py': ROOT / 'runner/agentic/quality.py',
        data / 'runner/agentic/receipts.py': ROOT / 'runner/agentic/receipts.py',
        data / 'runner/agentic/review.py': ROOT / 'runner/agentic/review.py',
        data / 'runner/agentic/sources.py': ROOT / 'runner/agentic/sources.py',
        data / 'runner/agentic/storage.py': ROOT / 'runner/agentic/storage.py',
        data / 'runner/agentic/tasks.py': ROOT / 'runner/agentic/tasks.py',
        data / 'runner/agentic/tdd.py': ROOT / 'runner/agentic/tdd.py',
        data / 'runner/agentic/wheel.py': ROOT / 'runner/agentic/wheel.py',
    }


def legacy_targets() -> dict[Path, Path]:
    """Return the exact destination layout shipped by the v0.8.0 installer."""
    config, data, _ = directories()
    legacy_config, legacy_data, _ = legacy_directories()
    mapping = {}
    for dest, source in targets().items():
        if dest.is_relative_to(config / 'plugins/agentic-kit'):
            relative = dest.relative_to(config / 'plugins/agentic-kit')
            legacy_dest = legacy_config / 'plugins/agentic-preview' / relative
        elif dest.is_relative_to(data):
            legacy_dest = legacy_data / dest.relative_to(data)
        else:
            legacy_dest = dest
        mapping[legacy_dest] = source
    return mapping


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f'UNSAFE_PATH: {path}: {detail}')


def _create_directory_component(parent: int, component: str, path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        os.mkdir(component, 0o755, dir_fd=parent)
        return os.open(component, flags, dir_fd=parent)
    except OSError as exc:
        raise _path_error(path, f'cannot safely create directory component: {component}') from exc


def _open_directory_component(
    parent: int, component: str, path: Path, *, create: bool, missing_ok: bool
) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(component, flags, dir_fd=parent)
    except FileNotFoundError:
        if create:
            return _create_directory_component(parent, component, path)
        if missing_ok:
            return None
        raise _path_error(path, f'missing directory component: {component}')
    except OSError as exc:
        raise _path_error(path, f'non-directory or symlink component: {component}') from exc


def open_directory(path: Path, *, create: bool = False, missing_ok: bool = False) -> int | None:
    """Open an absolute directory without following any path-component symlink."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise _path_error(path, 'managed paths must be absolute and normalized')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            child = _open_directory_component(
                descriptor, component, path, create=create, missing_ok=missing_ok
            )
            if child is None:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_directory(path: Path) -> None:
    descriptor = open_directory(path, create=True)
    assert descriptor is not None
    os.close(descriptor)


def managed_read(path: Path, *, missing_ok: bool = False) -> bytes | None:
    parent = open_directory(path.parent, missing_ok=missing_ok)
    if parent is None:
        return None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise _path_error(path, 'managed file is missing')
        except OSError as exc:
            raise _path_error(path, 'cannot safely open managed file') from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _path_error(path, 'managed file is not a regular file')
            chunks = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def managed_exists(path: Path) -> bool:
    return managed_read(path, missing_ok=True) is not None


def atomic_write(dest: Path, content: bytes, *, exclusive: bool = False):
    parent = open_directory(dest.parent, create=True)
    assert parent is not None
    temp_name = f'.installer-{os.getpid()}-{secrets.token_hex(8)}.tmp'
    temp_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent)
        temp_created = True
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if exclusive:
            try:
                os.link(
                    temp_name,
                    dest.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise RuntimeError(
                    f'BACKUP_EXISTS: refusing to overwrite retained backup: {dest}'
                ) from exc
        else:
            try:
                metadata = os.stat(dest.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise _path_error(dest, 'managed destination is not a regular file')
            os.replace(temp_name, dest.name, src_dir_fd=parent, dst_dir_fd=parent)
            temp_created = False
        os.fsync(parent)
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def durable_unlink(dest: Path) -> None:
    parent = open_directory(dest.parent)
    assert parent is not None
    try:
        try:
            metadata = os.stat(dest.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _path_error(dest, 'managed file is missing') from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise _path_error(dest, 'managed destination is not a regular file')
        os.unlink(dest.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def managed_unlink(dest: Path, *, missing_ok: bool = False) -> None:
    if missing_ok and not managed_exists(dest):
        return
    durable_unlink(dest)


def restore_content(dest: Path, content: bytes | None) -> None:
    """Restore a prior regular file, or remove a target created by the installer."""
    if content is None:
        durable_unlink(dest)
    else:
        atomic_write(dest, content)


def write_new_backups(backups: list[tuple[Path, bytes]], *, checkpoint: str | None = None) -> None:
    """Create every backup without ever replacing a retained backup."""
    for path, _ in backups:
        if managed_exists(path):
            raise RuntimeError(f'BACKUP_EXISTS: refusing to overwrite retained backup: {path}')
    for path, content in backups:
        atomic_write(path, content, exclusive=True)
        if checkpoint is not None:
            install_checkpoint(checkpoint, path)


def install_checkpoint(stage: str, dest: Path | None = None) -> None:
    """Test seam for simulating abrupt stops at durable install boundaries."""


def _require_transaction(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(f'INVALID_TRANSACTION: {detail}')


def _encoded_previous_manifest(previous_text: object) -> bytes | None:
    if previous_text is None:
        return None
    _require_transaction(isinstance(previous_text, str), 'malformed install recovery state')
    return cast(str, previous_text).encode('utf-8')


class InstallTransaction:
    """Durable recovery state spanning TUI config, release targets, and manifest."""

    def __init__(self, state: Path):
        self.state = state
        self.manifest = state / 'manifest.json'
        self.journal = state / 'transaction.json'

    def require_idle(self) -> None:
        if managed_exists(self.journal):
            raise RuntimeError(
                'INCOMPLETE_TRANSACTION: run install.py --rollback to recover '
                'the interrupted install or upgrade'
            )

    def create_backup_generation(self, root: Path, purpose: str) -> Path:
        """Create a transaction-owned directory without adopting an existing path."""
        parent = open_directory(root, create=True)
        assert parent is not None
        try:
            while True:
                name = f'{purpose}-{secrets.token_hex(8)}'
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    continue
                os.fsync(parent)
                return root / name
        finally:
            os.close(parent)

    def prepare(
        self,
        operation: str,
        targets_state: list[dict],
        final_manifest: bytes,
        previous_manifest: bytes | None,
    ) -> None:
        self.require_idle()
        data = {
            'format': 1,
            'operation': operation,
            'manifest': str(self.manifest),
            'previous_manifest': (
                previous_manifest.decode('utf-8') if previous_manifest is not None else None
            ),
            'previous_manifest_hash': (
                digest(previous_manifest) if previous_manifest is not None else None
            ),
            'final_manifest_hash': digest(final_manifest),
            'targets': targets_state,
        }
        atomic_write(self.journal, (json.dumps(data, indent=2) + '\n').encode())
        install_checkpoint('after_transaction_prepare')

    def commit(self) -> None:
        durable_unlink(self.journal)

    def _previous_state(
        self,
        operation: str,
        previous_text: object,
        previous_hash: object,
        mapping: dict[Path, Path],
    ) -> bytes | None:
        previous = _encoded_previous_manifest(previous_text)
        if operation == 'install':
            _require_transaction(previous is None, 'invalid previous manifest state')
            _require_transaction(previous_hash is None, 'invalid previous manifest state')
            return None
        _require_transaction(isinstance(previous_hash, str), 'invalid previous manifest state')
        previous_bytes = cast(bytes, previous)
        _require_transaction(
            digest(previous_bytes) == previous_hash, 'invalid previous manifest state'
        )
        previous_data = json.loads(previous_bytes)
        validate_rollback_manifest(previous_data, mapping)
        _require_transaction(
            previous_data.get('rollback') is None,
            'upgrade cannot restore rollback progress',
        )
        return previous_bytes

    def _load_recovery(self, content: bytes, mapping: dict[Path, Path]) -> tuple:
        try:
            data = json.loads(content)
            operation = data['operation']
            entries = data['targets']
            previous_hash = data['previous_manifest_hash']
            final_hash = data['final_manifest_hash']
            _require_transaction(data.get('format') == 1, 'malformed install recovery state')
            _require_transaction(
                operation in {'install', 'upgrade'}, 'malformed install recovery state'
            )
            _require_transaction(
                data.get('manifest') == str(self.manifest), 'malformed install recovery state'
            )
            _require_transaction(isinstance(entries, list), 'malformed install recovery state')
            _require_transaction(isinstance(final_hash, str), 'malformed install recovery state')
            previous = self._previous_state(
                operation, data['previous_manifest'], previous_hash, mapping
            )
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise RuntimeError('INVALID_TRANSACTION: malformed install recovery state') from exc
        return operation, entries, previous, previous_hash, final_hash

    def _validate_recovery_destinations(
        self, entries: list[dict], mapping: dict[Path, Path]
    ) -> None:
        config, _, _ = directories()
        expected = {str(path) for path in mapping} | {str(config / 'cli.json')}
        destinations = [entry.get('dest') for entry in entries]
        _require_transaction(
            len(destinations) == len(set(destinations)),
            'recovery targets do not match managed targets',
        )
        _require_transaction(
            set(destinations) == expected,
            'recovery targets do not match managed targets',
        )

    def _is_recovery_backup(self, backup: Path) -> bool:
        roots = (self.state / 'backups', self.state / 'transaction-backups')
        return any(backup.parent.is_relative_to(root) for root in roots)

    def _recovery_target_fields(self, entry: dict) -> tuple:
        try:
            dest = Path(entry['dest'])
            existed = entry['existed']
            installed_hash = entry['installed_hash']
            backup_value = entry['backup']
            backup_hash = entry['backup_hash']
            _require_transaction(isinstance(existed, bool), 'malformed target state')
            _require_transaction(isinstance(installed_hash, str), 'malformed target state')
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError('INVALID_TRANSACTION: malformed target state') from exc
        return dest, existed, backup_value, backup_hash

    @staticmethod
    def _require_recovery_backup(backup: Path, content: bytes | None) -> bytes:
        if content is None:
            raise RuntimeError(f'BACKUP_MISSING: {backup}')
        return content

    @staticmethod
    def _verify_recovery_backup(backup: Path, content: bytes, backup_hash: str) -> None:
        if digest(content) != backup_hash:
            raise RuntimeError(f'BACKUP_CHANGED: {backup}; refusing unsafe recovery')

    def _recovery_original(self, entry: dict) -> tuple[Path, bytes | None]:
        dest, existed, backup_value, backup_hash = self._recovery_target_fields(entry)
        if not existed:
            _require_transaction(backup_value is None, 'absent target has backup metadata')
            _require_transaction(backup_hash is None, 'absent target has backup metadata')
            return dest, None
        _require_transaction(isinstance(backup_value, str), 'existing target lacks backup metadata')
        _require_transaction(isinstance(backup_hash, str), 'existing target lacks backup metadata')
        backup = Path(backup_value)
        _require_transaction(self._is_recovery_backup(backup), f'unexpected backup path: {backup}')
        content = self._require_recovery_backup(backup, managed_read(backup, missing_ok=True))
        self._verify_recovery_backup(backup, content, backup_hash)
        return dest, content

    def _recovery_originals(self, entries: list[dict]) -> dict[Path, bytes | None]:
        restored = {}
        for entry in entries:
            dest, original = self._recovery_original(entry)
            restored[dest] = original
        return restored

    @staticmethod
    def _content_hash(content: bytes | None) -> str | None:
        return digest(content) if content is not None else None

    def _validate_recovery_target(self, entry: dict, original: bytes | None) -> None:
        dest = Path(entry['dest'])
        current_hash = self._content_hash(managed_read(dest, missing_ok=True))
        backup_hash = entry['backup_hash'] if original is not None else None
        restored_hash = self._content_hash(original)
        if current_hash not in {backup_hash, restored_hash, entry['installed_hash']}:
            raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite post-crash edits')

    def _validate_recovery_targets(
        self, entries: list[dict], restored: dict[Path, bytes | None]
    ) -> None:
        for entry in entries:
            self._validate_recovery_target(entry, restored[Path(entry['dest'])])

    def _validate_recovery_manifest(
        self, previous_hash: str | None, final_hash: str
    ) -> bytes | None:
        current = managed_read(self.manifest, missing_ok=True)
        if current is None:
            return None
        valid_hashes = {final_hash, previous_hash} - {None}
        if digest(current) not in valid_hashes:
            raise RuntimeError(
                f'LOCAL_CHANGE: {self.manifest}; refusing to overwrite changed recovery state'
            )
        return current

    def _restore_recovery_target(self, entry: dict, original: bytes | None) -> None:
        dest = Path(entry['dest'])
        current_hash = self._content_hash(managed_read(dest, missing_ok=True))
        restored_hash = self._content_hash(original)
        if current_hash == restored_hash:
            return
        restore_content(dest, original)
        install_checkpoint('after_recovery_target', dest)

    @staticmethod
    def _without_recovered_tui_registration(
        restored: dict[Path, bytes | None],
    ) -> dict[Path, bytes | None]:
        cli = directories()[0] / 'cli.json'
        content = restored.get(cli)
        if content is not None:
            restored[cli] = _without_kit_tui_registrations(content, cli)
        return restored

    def _restore_recovery_targets(
        self, entries: list[dict], restored: dict[Path, bytes | None]
    ) -> None:
        for entry in entries:
            self._restore_recovery_target(entry, restored[Path(entry['dest'])])

    def _remove_install_manifest(self) -> None:
        if managed_exists(self.manifest):
            durable_unlink(self.manifest)

    def _restore_upgrade_manifest(
        self, previous: bytes, previous_hash: str, current: bytes | None
    ) -> None:
        if self._content_hash(current) != previous_hash:
            atomic_write(self.manifest, previous)

    def _restore_recovery_manifest(
        self, previous: bytes | None, previous_hash: str | None, current: bytes | None
    ) -> None:
        if previous is None:
            self._remove_install_manifest()
            return
        assert previous_hash is not None
        self._restore_upgrade_manifest(previous, previous_hash, current)

    def recover(self, mapping: dict[Path, Path]) -> bool:
        content = managed_read(self.journal, missing_ok=True)
        if content is None:
            return False
        operation, entries, previous, previous_hash, final_hash = self._load_recovery(
            content, mapping
        )
        self._validate_recovery_destinations(entries, mapping)
        restored = self._without_recovered_tui_registration(self._recovery_originals(entries))
        self._validate_recovery_targets(entries, restored)
        current_manifest = self._validate_recovery_manifest(previous_hash, final_hash)
        self._restore_recovery_targets(entries, restored)
        self._restore_recovery_manifest(previous, previous_hash, current_manifest)
        durable_unlink(self.journal)
        print(f'RECOVERED: interrupted {operation} restored its previous state')
        return True


def archive_checkpoint(stage: str, archive: Path | None = None) -> None:
    """Test seam for simulating stops at durable backup archive boundaries."""


def _open_child_directory(parent: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
            return os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            raise _path_error(Path(name), 'cannot safely create directory') from exc
    except OSError as exc:
        raise _path_error(Path(name), 'non-directory or symlink') from exc


def _directory_has_entries(descriptor: int) -> bool:
    with os.scandir(descriptor) as entries:
        return next(entries, None) is not None


def _rename_noreplace(
    source: str, destination: str, *, source_dir: int, destination_dir: int
) -> None:
    """Atomically rename a directory without replacing any existing archive."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if renameat2 is None:
        raise RuntimeError('UNSAFE_ARCHIVE: atomic no-replace rename is unavailable')
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(source_dir, os.fsencode(source), destination_dir, os.fsencode(destination), 1)
        == 0
    ):  # RENAME_NOREPLACE
        return
    error = ctypes.get_errno()
    error_type = {errno.EEXIST: FileExistsError}.get(error, OSError)
    error_path = {errno.EEXIST: destination}.get(error, source)
    raise error_type(error, os.strerror(error), error_path)


def prepare_backup_directory(state: Path) -> Path:
    """Archive a non-empty prior backup generation and return a fresh directory."""
    state_descriptor = open_directory(state)
    assert state_descriptor is not None
    backups = state / 'backups'
    try:
        backup_descriptor = _open_child_directory(state_descriptor, backups.name)
        try:
            if not _directory_has_entries(backup_descriptor):
                return backups
        finally:
            os.close(backup_descriptor)

        archive_root = state / 'backup-archives'
        archive_descriptor = _open_child_directory(state_descriptor, archive_root.name)
        try:
            while True:
                archive_name = f'backup-{secrets.token_hex(8)}'
                try:
                    _rename_noreplace(
                        backups.name,
                        archive_name,
                        source_dir=state_descriptor,
                        destination_dir=archive_descriptor,
                    )
                except FileExistsError:
                    continue
                break
            os.fsync(archive_descriptor)
            os.fsync(state_descriptor)
        finally:
            os.close(archive_descriptor)
        archive = archive_root / archive_name
        archive_checkpoint('after_backup_archive', archive)
        new_backup_descriptor = _open_child_directory(state_descriptor, backups.name)
        os.close(new_backup_descriptor)
        archive_checkpoint('after_backup_directory_create', archive)
        return backups
    finally:
        os.close(state_descriptor)


@contextmanager
def installer_lock(state: Path | None = None):
    """Serialize installer transactions across processes using one stable inode."""
    if state is None:
        _, _, state = directories()
    ensure_directory(state)
    lock_path = state / 'installer.lock'
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    parent = open_directory(state)
    assert parent is not None
    try:
        descriptor = os.open(lock_path.name, flags, 0o600, dir_fd=parent)
    except OSError as exc:
        os.close(parent)
        raise RuntimeError(f'UNSAFE_LOCK: cannot safely open installer lock: {lock_path}') from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError(f'UNSAFE_LOCK: insecure installer lock metadata: {lock_path}')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f'INSTALLER_BUSY: another install, upgrade, rollback, or uninstall owns {lock_path}'
            ) from exc
        yield
    finally:
        os.close(descriptor)
        os.close(parent)


@contextmanager
def uninstall_locks():
    """Exclude operations in both legacy v0.8.0 and canonical v0.8.1+ layouts."""
    current_state = directories()[2]
    legacy_state = legacy_directories()[2]
    with installer_lock(current_state), installer_lock(legacy_state):
        yield


def _existing_target_status(current: bytes, expected: bytes) -> str:
    return 'UNCHANGED' if current == expected else 'BACKUP+REPLACE'


def _target_status(dest: Path, source: Path) -> str:
    current = managed_read(dest, missing_ok=True)
    if current is None:
        return 'CREATE'
    return _existing_target_status(current, source.read_bytes())


def _print_plan_summary(state: Path) -> None:
    if managed_exists(state / 'manifest.json'):
        print('Existing managed installation detected; changed sources require explicit --upgrade.')
    print(f'Backup state: {state}')
    print(f'Source tree: {ROOT}')
    print('Agentic Kit conflicts with the lab plugin if both are active in the lab project.')


def plan():
    require_no_legacy_installation()
    for dest, source in targets().items():
        status = _target_status(dest, source)
        print(f'{status:16s} {dest}')
    tui_dest, tui_original, tui_content = tui_config_patch()
    tui_status = {
        (True, True): 'UNCHANGED',
        (True, False): 'UNCHANGED',
        (False, True): 'CREATE',
        (False, False): 'PATCH',
    }[(tui_original == tui_content, tui_original is None)]
    print(f'{tui_status:16s} {tui_dest} (remove duplicate Agentic Kit registrations)')
    _, _, state = directories()
    _print_plan_summary(state)


def source_bytes(mapping: dict[Path, Path]) -> dict[Path, bytes]:
    """Read all source files before touching any installation target."""
    content = {}
    for dest, source in mapping.items():
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f'INVALID_SOURCE: {source}')
        content[dest] = source.read_bytes()
    return content


def _valid_plugin_directive(item: object) -> bool:
    if isinstance(item, (str, list)):
        return True
    return isinstance(item, dict) and isinstance(item.get('package'), str)


def _plugin_directives(data: dict, dest: Path) -> list:
    plugins = data.setdefault('plugins', [])
    if not isinstance(plugins, list):
        raise RuntimeError(  # noqa: TRY004 -- external config errors keep the CLI contract.
            f'INVALID_TUI_CONFIG: {dest} plugins must be a list of package directives'
        )
    if not all(_valid_plugin_directive(item) for item in plugins):
        raise RuntimeError(
            f'INVALID_TUI_CONFIG: {dest} plugins must be a list of package directives'
        )
    return plugins


def _plugin_package(item: object) -> object:
    if isinstance(item, str):
        return item
    if isinstance(item, list) and item:
        return item[0]
    if isinstance(item, dict):
        return item.get('package')
    return None


def _is_kit_tui_registration(item: object, config: Path) -> bool:
    legacy = config / 'plugins/agentic-preview'
    current = config / 'plugins/agentic-kit'
    legacy_tui = legacy / 'tui.ts'
    current_tui = current / 'tui.ts'
    specs = {
        LEGACY_TUI_PLUGIN_SPEC,
        TUI_PLUGIN_SPEC,
        'opencode-agentic-kit',
        'opencode-agentic-kit-preview',
        str(legacy),
        str(legacy_tui),
        str(current),
        str(current_tui),
        f'file://{legacy}',
        f'file://{legacy_tui}',
        f'file://{current}',
        f'file://{current_tui}',
    }
    return _plugin_package(item) in specs


def _without_kit_tui_registrations(content: bytes, dest: Path) -> bytes:
    try:
        data = json.loads(content.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'INVALID_TUI_CONFIG: {dest} must be valid JSON') from exc
    if not isinstance(data, dict):
        raise RuntimeError(  # noqa: TRY004 -- external config errors keep the CLI contract.
            f'INVALID_TUI_CONFIG: {dest} must contain a JSON object'
        )
    if 'plugins' not in data:
        return content
    plugins = _plugin_directives(data, dest)
    before = list(plugins)
    config, _, _ = directories()
    plugins[:] = [item for item in plugins if not _is_kit_tui_registration(item, config)]
    if plugins == before:
        return content
    return (json.dumps(data, indent=2) + '\n').encode()


def tui_config_patch() -> tuple[Path, bytes | None, bytes]:
    config, _, _ = directories()
    dest = config / 'cli.json'
    original = managed_read(dest, missing_ok=True)
    content = original
    if content is None:
        content = (
            json.dumps({'$schema': 'https://opencode.ai/v2/cli.json'}, indent=2) + '\n'
        ).encode()
    content = _without_kit_tui_registrations(content, dest)
    return dest, original, content


def _manifest_entries(manifest_data: dict) -> list[dict]:
    entries = manifest_data['files']
    if not isinstance(entries, list):
        raise RuntimeError(  # noqa: TRY004 -- persisted-state errors keep the CLI contract.
            'INVALID_MANIFEST: installed file list must be a list'
        )
    return entries


def _validate_manifest_destinations(entries: list[dict], mapping: dict[Path, Path]) -> None:
    destinations = [entry['dest'] for entry in entries]
    if len(destinations) != len(set(destinations)):
        raise RuntimeError('INVALID_MANIFEST: installed file list is not a known target subset')
    if not set(destinations) <= {str(dest) for dest in mapping}:
        raise RuntimeError('INVALID_MANIFEST: installed file list is not a known target subset')


def _check_managed_entry(entry: dict) -> None:
    dest = Path(entry['dest'])
    current = managed_read(dest, missing_ok=True)
    if current is None or digest(current) != entry['installed_hash']:
        raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite local edits')


def _check_managed_tui(tui: dict | None) -> None:
    if tui is None:
        return
    dest = Path(tui['dest'])
    if dest != directories()[0] / 'cli.json':
        raise RuntimeError('INVALID_MANIFEST: invalid TUI config target')
    _check_managed_entry(tui)


def check_managed(manifest_data: dict, mapping: dict[Path, Path]) -> None:
    """A manifest may be an older target subset, but never contain unknown/edited files."""
    try:
        entries = _manifest_entries(manifest_data)
        _validate_manifest_destinations(entries, mapping)
        if manifest_data.get('rollback') is not None:
            validate_rollback_state(manifest_data)
            raise RuntimeError('ROLLBACK_IN_PROGRESS: resume --rollback before install or upgrade')
        for entry in entries:
            _check_managed_entry(entry)
        _check_managed_tui(manifest_data.get('tui_config'))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed Agentic Kit installation state') from exc


def rollback_entries(manifest_data: dict) -> list[dict]:
    entries = manifest_data['files']
    if not isinstance(entries, list):
        raise RuntimeError(  # noqa: TRY004 -- persisted-state errors keep the CLI contract.
            'INVALID_MANIFEST: installed file list must be a list'
        )
    result = list(entries)
    tui = manifest_data.get('tui_config')
    if tui is not None:
        result.append(tui)
    return result


def _require_manifest(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(f'INVALID_MANIFEST: {detail}')


def validate_rollback_state(manifest_data: dict) -> None:
    try:
        rollback_state = manifest_data['rollback']
        completed = rollback_state['completed']
        restored_hashes = rollback_state['restored_hashes']
        destinations = [entry['dest'] for entry in rollback_entries(manifest_data)]
        detail = 'malformed rollback progress'
        _require_manifest(isinstance(rollback_state, dict), detail)
        _require_manifest(rollback_state.get('status') == 'in_progress', detail)
        _require_manifest(isinstance(completed, list), detail)
        _require_manifest(all(isinstance(dest, str) for dest in completed), detail)
        _require_manifest(len(completed) == len(set(completed)), detail)
        _require_manifest(set(completed) <= set(destinations), detail)
        _require_manifest(isinstance(restored_hashes, dict), detail)
        _require_manifest(set(restored_hashes) == set(destinations), detail)
        _require_manifest(
            all(value is None or isinstance(value, str) for value in restored_hashes.values()),
            detail,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed rollback progress') from exc


def rollback_checkpoint(stage: str, dest: Path | None = None) -> None:
    """Test seam for simulating an abrupt stop at durable rollback boundaries."""


def _valid_backup_name(backup: Path, expected_name: str | None) -> bool:
    if expected_name is not None:
        return backup.name == expected_name
    return backup.suffix == '.bak' and backup.stem.isdigit()


def _valid_backup_parent(backup: Path, backup_dir: Path) -> bool:
    if backup.parent == backup_dir:
        return True
    generation = backup.parent.name
    token = generation.removeprefix('upgrade-')
    return (
        backup.parent.parent == backup_dir
        and generation.startswith('upgrade-')
        and len(token) == 16
        and all(character in '0123456789abcdef' for character in token)
    )


def _rollback_backup_metadata(entry: dict) -> tuple[bool, object, object]:
    existed = entry['existed']
    backup_value = entry['backup']
    _require_manifest(isinstance(existed, bool), 'target existed flag must be boolean')
    if not existed:
        _require_manifest(backup_value is None, 'absent target must not have backup metadata')
        _require_manifest(
            entry.get('backup_hash') is None, 'absent target must not have backup metadata'
        )
    return existed, backup_value, entry.get('backup_hash')


def _verified_backup(backup: Path, trusted_hash: str) -> bytes:
    content = managed_read(backup, missing_ok=True)
    if content is None:
        raise RuntimeError(f'BACKUP_MISSING: {backup}')
    if digest(content) != trusted_hash:
        raise RuntimeError(f'BACKUP_CHANGED: {backup}; refusing unsafe rollback')
    return content


def rollback_backup(
    entry: dict, backup_dir: Path, expected_name: str | None = None
) -> bytes | None:
    try:
        existed, backup_value, trusted_hash = _rollback_backup_metadata(entry)
        if not existed:
            return None
        if not isinstance(trusted_hash, str):
            raise RuntimeError(  # noqa: TRY004 -- missing trust data is an operational failure.
                'UNVERIFIED_BACKUP: legacy manifest has no trusted original hash; '
                'managed targets were not changed. Restore the retained backup manually after review.'
            )
        backup = Path(cast(str, backup_value))
        if not _valid_backup_parent(backup, backup_dir):
            raise RuntimeError(f'INVALID_MANIFEST: unexpected backup path: {backup}')
        if not _valid_backup_name(backup, expected_name):
            raise RuntimeError(f'INVALID_MANIFEST: unexpected backup path: {backup}')
        return _verified_backup(backup, trusted_hash)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed backup metadata') from exc


def rollback_target_state(entry: dict, restored: bytes | None) -> str:
    dest = Path(entry['dest'])
    current = managed_read(dest, missing_ok=True)
    if restored is None:
        if current is None:
            return 'restored'
    elif current is not None and digest(current) == digest(restored):
        return 'restored'
    if current is not None and digest(current) == entry['installed_hash']:
        return 'installed'
    return 'unknown'


def validate_rollback_manifest(manifest_data: dict, mapping: dict[Path, Path]) -> None:
    try:
        entries = _manifest_entries(manifest_data)
        destinations = [entry['dest'] for entry in entries]
        detail = 'installed file list is not a known target subset'
        _require_manifest(all(isinstance(dest, str) for dest in destinations), detail)
        _require_manifest(len(destinations) == len(set(destinations)), detail)
        _require_manifest(set(destinations) <= {str(dest) for dest in mapping}, detail)
        for entry in entries:
            _require_manifest(
                isinstance(entry.get('installed_hash'), str), 'missing installed file hash'
            )
        tui = manifest_data.get('tui_config')
        if tui is not None:
            expected = directories()[0] / 'cli.json'
            _require_manifest(tui.get('dest') == str(expected), 'invalid TUI config target')
            _require_manifest(
                isinstance(tui.get('installed_hash'), str), 'invalid TUI config target'
            )
        if manifest_data.get('rollback') is not None:
            validate_rollback_state(manifest_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed Agentic Kit installation state') from exc


def write_release(
    mapping: dict[Path, Path],
    content: dict[Path, bytes],
    manifest: Path,
    entries: list[dict],
    prior_bytes: dict[Path, bytes],
    tui_entry: dict,
):
    """Apply release targets and its completed manifest under a prepared journal."""
    for dest in mapping:
        if dest in prior_bytes and prior_bytes[dest] == content[dest]:
            continue
        atomic_write(dest, content[dest])
        install_checkpoint('after_target_change', dest)
    install_checkpoint('before_manifest_commit')
    data = {
        'version': INSTALLER_COMPATIBILITY_VERSION,
        'product_version': PRODUCT_VERSION,
        'files': entries,
        'tui_config': tui_entry,
    }
    atomic_write(manifest, (json.dumps(data, indent=2) + '\n').encode())
    install_checkpoint('after_manifest_commit')


def transaction_target_entry(
    dest: Path, original: bytes | None, installed: bytes, backup: Path | None
) -> dict:
    return {
        'dest': str(dest),
        'existed': original is not None,
        'backup': str(backup) if backup is not None else None,
        'backup_hash': digest(original) if original is not None else None,
        'installed_hash': digest(installed),
    }


def upgrade_recovery_state(
    transaction: InstallTransaction, originals: dict[Path, bytes], intended: dict[Path, bytes]
) -> list[dict]:
    generation = transaction.create_backup_generation(
        transaction.state / 'transaction-backups', 'recovery'
    )
    pending: list[tuple[Path, bytes]] = []
    entries = []
    for index, (dest, installed) in enumerate(intended.items()):
        original = originals.get(dest)
        backup = generation / f'{index}.bak' if original is not None else None
        if backup is not None:
            pending.append((backup, cast(bytes, original)))
        entries.append(transaction_target_entry(dest, original, installed, backup))
    write_new_backups(pending, checkpoint='after_upgrade_recovery_backup')
    return entries


def install(*, upgrade: bool = False):
    require_no_legacy_installation()
    with installer_lock():
        _install(upgrade=upgrade)


def _manifest_bytes(entries: list[dict], tui_entry: dict) -> bytes:
    data = {
        'version': INSTALLER_COMPATIBILITY_VERSION,
        'product_version': PRODUCT_VERSION,
        'files': entries,
        'tui_config': tui_entry,
    }
    return (json.dumps(data, indent=2) + '\n').encode()


def _file_entry(
    dest: Path, source: Path, original: bytes | None, backup: Path | None, installed: bytes
) -> dict:
    return {
        'dest': str(dest),
        'source': str(source),
        'existed': original is not None,
        'backup': str(backup) if backup else None,
        'backup_hash': digest(original) if original is not None else None,
        'installed_hash': digest(installed),
    }


def _tui_entry(
    dest: Path, original: bytes | None, backup: Path | None, installed: bytes | None = None
) -> dict:
    entry = {
        'dest': str(dest),
        'existed': original is not None,
        'backup': str(backup) if backup else None,
        'backup_hash': digest(original) if original is not None else None,
    }
    if installed is not None:
        entry['installed_hash'] = digest(installed)
    return entry


def _installation_matches(
    prior: dict,
    mapping: dict[Path, Path],
    content: dict[Path, bytes],
    tui_dest: Path,
    tui_content: bytes,
) -> bool:
    if len(prior['files']) != len(mapping):
        return False
    if prior.get('tui_config') is None:
        return False
    for entry in prior['files']:
        dest = Path(entry['dest'])
        if digest(content[dest]) != entry['installed_hash']:
            return False
    return managed_read(tui_dest) == tui_content


def _current_targets(mapping: dict[Path, Path]) -> dict[Path, bytes]:
    current = {}
    for dest in mapping:
        value = managed_read(dest, missing_ok=True)
        if value is not None:
            current[dest] = value
    return current


def _prepare_upgrade_tui(
    prior: dict,
    backups: Path,
    tui_dest: Path,
    tui_original: bytes | None,
    pending_backups: list[tuple[Path, bytes]],
) -> dict:
    existing = prior.get('tui_config')
    if existing is not None:
        return existing
    tui_backup = backups / 'cli.json.bak' if tui_original is not None else None
    if tui_backup is not None:
        pending_backups.append((tui_backup, cast(bytes, tui_original)))
    return _tui_entry(tui_dest, tui_original, tui_backup)


def _append_upgrade_entries(
    entries: list[dict],
    mapping: dict[Path, Path],
    content: dict[Path, bytes],
    current: dict[Path, bytes],
    backups: Path,
    pending_backups: list[tuple[Path, bytes]],
) -> None:
    installed = {Path(entry['dest']) for entry in entries}
    for dest, source in mapping.items():
        if dest in installed:
            continue
        original = current.get(dest)
        saved = backups / f'{len(entries)}.bak' if original is not None else None
        if saved is not None:
            pending_backups.append((saved, cast(bytes, original)))
        entries.append(_file_entry(dest, source, original, saved, content[dest]))


def _refresh_upgrade_entries(
    entries: list[dict], mapping: dict[Path, Path], content: dict[Path, bytes]
) -> None:
    for entry in entries:
        dest = Path(entry['dest'])
        entry['source'] = str(mapping[dest])
        entry['installed_hash'] = digest(content[dest])


def _apply_tui(dest: Path, original: bytes | None, intended: bytes) -> None:
    if original == intended:
        return
    atomic_write(dest, intended)
    install_checkpoint('after_target_change', dest)


def _upgrade_install(
    mapping: dict[Path, Path],
    content: dict[Path, bytes],
    state: Path,
    manifest: Path,
    transaction: InstallTransaction,
    manifest_content: bytes,
    prior: dict,
    tui_dest: Path,
    tui_original: bytes | None,
    tui_content: bytes,
) -> None:
    # Original backups and existed flags stay untouched. --rollback must
    # restore files from before the first managed install, not the previous release.
    current = _current_targets(mapping)
    entries = [dict(entry) for entry in prior['files']]
    backups = state / 'backups'
    ensure_directory(backups)
    installed = {Path(entry['dest']) for entry in entries}
    needs_new_backup = (prior.get('tui_config') is None and tui_original is not None) or any(
        dest not in installed and dest in current for dest in mapping
    )
    new_backups = (
        transaction.create_backup_generation(backups, 'upgrade') if needs_new_backup else backups
    )
    pending_backups: list[tuple[Path, bytes]] = []
    tui_entry = _prepare_upgrade_tui(prior, new_backups, tui_dest, tui_original, pending_backups)
    _append_upgrade_entries(entries, mapping, content, current, new_backups, pending_backups)
    install_checkpoint('before_upgrade_backups')
    write_new_backups(pending_backups, checkpoint='after_upgrade_backup')
    install_checkpoint('after_upgrade_backups')
    _refresh_upgrade_entries(entries, mapping, content)
    tui_entry['installed_hash'] = digest(tui_content)
    final_manifest = _manifest_bytes(entries, tui_entry)
    recovery_originals = dict(current)
    if tui_original is not None:
        recovery_originals[tui_dest] = tui_original
    intended = {tui_dest: tui_content, **content}
    recovery_entries = upgrade_recovery_state(transaction, recovery_originals, intended)
    transaction.prepare('upgrade', recovery_entries, final_manifest, manifest_content)
    _apply_tui(tui_dest, tui_original, tui_content)
    write_release(mapping, content, manifest, entries, current, tui_entry)
    transaction.commit()
    print(
        f'UPGRADED: {PRODUCT_VERSION}. Original rollback backups preserved; restart OpenCode. '
        'To remove Agentic Kit, run: python3 install.py --uninstall'
    )


def _new_install_entries(
    mapping: dict[Path, Path], content: dict[Path, bytes], backups: Path
) -> tuple[list[dict], dict[Path, bytes], list[tuple[Path, bytes]]]:
    entries = []
    previous = {}
    pending_backups: list[tuple[Path, bytes]] = []
    for index, (dest, source) in enumerate(mapping.items()):
        original = managed_read(dest, missing_ok=True)
        saved = backups / f'{index}.bak' if original is not None else None
        if saved is not None:
            original_bytes = cast(bytes, original)
            pending_backups.append((saved, original_bytes))
            previous[dest] = original_bytes
        entries.append(_file_entry(dest, source, original, saved, content[dest]))
    return entries, previous, pending_backups


def _new_recovery_entries(
    entries: list[dict],
    previous: dict[Path, bytes],
    content: dict[Path, bytes],
    tui_dest: Path,
    tui_original: bytes | None,
    tui_content: bytes,
    tui_backup: Path | None,
) -> list[dict]:
    result = [transaction_target_entry(tui_dest, tui_original, tui_content, tui_backup)]
    for entry in entries:
        dest = Path(entry['dest'])
        backup = Path(entry['backup']) if entry['backup'] else None
        result.append(transaction_target_entry(dest, previous.get(dest), content[dest], backup))
    return result


def _new_install(
    mapping: dict[Path, Path],
    content: dict[Path, bytes],
    state: Path,
    manifest: Path,
    transaction: InstallTransaction,
    tui_dest: Path,
    tui_original: bytes | None,
    tui_content: bytes,
) -> None:
    for dest in mapping:
        managed_read(dest, missing_ok=True)
    ensure_directory(state)
    backups = prepare_backup_directory(state)
    entries, previous, pending_backups = _new_install_entries(mapping, content, backups)
    tui_backup = backups / 'cli.json.bak' if tui_original is not None else None
    if tui_backup is not None:
        pending_backups.append((tui_backup, cast(bytes, tui_original)))
    tui_entry = _tui_entry(tui_dest, tui_original, tui_backup, tui_content)
    write_new_backups(pending_backups)
    final_manifest = _manifest_bytes(entries, tui_entry)
    recovery_entries = _new_recovery_entries(
        entries, previous, content, tui_dest, tui_original, tui_content, tui_backup
    )
    transaction.prepare('install', recovery_entries, final_manifest, None)
    _apply_tui(tui_dest, tui_original, tui_content)
    write_release(mapping, content, manifest, entries, previous, tui_entry)
    transaction.commit()
    print(
        f'INSTALLED: {PRODUCT_VERSION}. Restart OpenCode. '
        'To remove Agentic Kit, run: python3 install.py --uninstall'
    )


def _install(*, upgrade: bool = False):
    mapping = targets()
    content = source_bytes(mapping)
    _, _, state = directories()
    manifest = state / 'manifest.json'
    transaction = InstallTransaction(state)
    transaction.require_idle()
    tui_dest, tui_original, tui_content = tui_config_patch()
    manifest_content = managed_read(manifest, missing_ok=True)
    if manifest_content is None:
        if upgrade:
            raise RuntimeError(
                'NOT_INSTALLED: --upgrade requires an existing preview; use --install'
            )
        _new_install(
            mapping,
            content,
            state,
            manifest,
            transaction,
            tui_dest,
            tui_original,
            tui_content,
        )
        return
    prior = json.loads(manifest_content)
    check_managed(prior, mapping)
    if _installation_matches(prior, mapping, content, tui_dest, tui_content):
        print('ALREADY_INSTALLED: installed files match this source tree')
        return
    if not upgrade:
        raise RuntimeError(
            'UPGRADE_REQUIRED: canonical sources changed; use --upgrade after review'
        )
    _upgrade_install(
        mapping,
        content,
        state,
        manifest,
        transaction,
        manifest_content,
        prior,
        tui_dest,
        tui_original,
        tui_content,
    )


def rollback():
    with installer_lock():
        _rollback()


def _rollback_contents(
    data: dict, backups: Path, *, discard_unverified: bool = False
) -> dict[str, bytes | None]:
    restored_content: dict[str, bytes | None] = {}
    for entry in data['files']:
        if discard_unverified and entry['existed'] and entry.get('backup_hash') is None:
            # Explicitly abandon an unverified original; remove only the managed target.
            restored_content[entry['dest']] = None
        else:
            restored_content[entry['dest']] = rollback_backup(entry, backups)
    tui = data.get('tui_config')
    if tui is not None:
        if discard_unverified and tui['existed'] and tui.get('backup_hash') is None:
            # Preserve unrelated current CLI settings, removing only Kit registrations.
            current = managed_read(Path(tui['dest']), missing_ok=True)
            restored = (
                _without_kit_tui_registrations(current, Path(tui['dest']))
                if current is not None
                else None
            )
        else:
            restored = rollback_backup(tui, backups, 'cli.json.bak')
            if restored is not None:
                restored = _without_kit_tui_registrations(restored, Path(tui['dest']))
        restored_content[tui['dest']] = restored
    return restored_content


def _trusted_original_hash(entry: dict) -> str | None:
    return entry.get('backup_hash') if entry['existed'] else None


def _begin_rollback(
    data: dict, entries: list[dict], manifest: Path, *, discard_unverified: bool = False
) -> None:
    restored_hashes = {entry['dest']: _trusted_original_hash(entry) for entry in entries}
    data['rollback'] = {
        'status': 'in_progress',
        'completed': [],
        'restored_hashes': restored_hashes,
    }
    if discard_unverified:
        data['rollback']['discard_unverified'] = True
    atomic_write(manifest, (json.dumps(data, indent=2) + '\n').encode())


def _validate_rollback_hashes(data: dict, entries: list[dict]) -> None:
    for entry in entries:
        trusted_hash = _trusted_original_hash(entry)
        if data['rollback']['restored_hashes'][entry['dest']] != trusted_hash:
            dest = entry['dest']
            raise RuntimeError(f'BACKUP_CHANGED: {dest}; refusing unsafe rollback')


def _validate_rollback_target(entry: dict, target_state: str, completed: set[str]) -> bool:
    dest = Path(entry['dest'])
    if target_state == 'unknown':
        raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite local edits')
    if entry['dest'] not in completed:
        return False
    if target_state != 'restored':
        raise RuntimeError(f'LOCAL_CHANGE: {dest}; completed rollback target was modified')
    return True


def _write_rollback_target(dest: Path, restored: bytes | None) -> None:
    restore_content(dest, restored)
    rollback_checkpoint('after_target_change', dest)


def _record_rollback_progress(data: dict, manifest: Path, dest: Path, completed: set[str]) -> None:
    rollback_checkpoint('before_progress', dest)
    data['rollback']['completed'].append(str(dest))
    atomic_write(manifest, (json.dumps(data, indent=2) + '\n').encode())
    completed.add(str(dest))
    rollback_checkpoint('after_progress', dest)


def _restore_rollback_targets(
    data: dict, entries: list[dict], manifest: Path, restored_content: dict[str, bytes | None]
) -> None:
    completed = set(data['rollback']['completed'])
    for entry in entries:
        dest = Path(entry['dest'])
        restored = restored_content[entry['dest']]
        target_state = rollback_target_state(entry, restored)
        if _validate_rollback_target(entry, target_state, completed):
            continue
        if target_state == 'installed':
            _write_rollback_target(dest, restored)
        _record_rollback_progress(data, manifest, dest, completed)


def _verify_rollback(entries: list[dict], restored_content: dict[str, bytes | None]) -> None:
    for entry in entries:
        dest = Path(entry['dest'])
        if rollback_target_state(entry, restored_content[entry['dest']]) != 'restored':
            raise RuntimeError(f'LOCAL_CHANGE: {dest}; final rollback verification failed')


def _rollback():
    _, _, state = directories()
    mapping = targets()
    if InstallTransaction(state).recover(mapping):
        return
    manifest = state / 'manifest.json'
    manifest_content = managed_read(manifest, missing_ok=True)
    if manifest_content is None:
        print('NOT_INSTALLED')
        return
    data = json.loads(manifest_content)
    validate_rollback_manifest(data, mapping)
    if data.get('rollback') is None:
        check_managed(data, mapping)
    entries = rollback_entries(data)
    restored_content = _rollback_contents(data, state / 'backups')
    if data.get('rollback') is None:
        _begin_rollback(data, entries, manifest)
    else:
        _validate_rollback_hashes(data, entries)
    _restore_rollback_targets(data, entries, manifest, restored_content)
    _verify_rollback(entries, restored_content)
    rollback_checkpoint('before_manifest_remove')
    durable_unlink(manifest)
    print('ROLLED_BACK: pre-installation files restored; restart OpenCode')


def uninstall_checkpoint(stage: str, path: Path | None = None) -> None:
    """Test seam for simulating abrupt stops at durable uninstall boundaries."""


def _rmdir_if_empty(path: Path) -> bool:
    """Remove one known directory without following a symlink."""
    parent = open_directory(path.parent, missing_ok=True)
    if parent is None:
        return True
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(metadata.st_mode):
            raise _path_error(path, 'cleanup target is not a directory')
        try:
            os.rmdir(path.name, dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                return False
            raise
        os.fsync(parent)
        return True
    finally:
        os.close(parent)


def _directory_is_nonempty(path: Path) -> bool:
    descriptor = open_directory(path, missing_ok=True)
    if descriptor is None:
        return False
    try:
        return _directory_has_entries(descriptor)
    finally:
        os.close(descriptor)


def _select_uninstall_layout() -> tuple[str, tuple[Path, Path, Path], dict[Path, Path]] | None:
    choices = [
        (PRODUCT_VERSION, directories(), targets()),
        ('0.8.0', legacy_directories(), legacy_targets()),
    ]
    installed = [choice for choice in choices if managed_exists(choice[1][2] / 'manifest.json')]
    if len(installed) > 1:
        raise RuntimeError(
            'MULTIPLE_INSTALLATIONS: both legacy v0.8.0 and canonical v0.8.1+ manifests exist; '
            'refusing ambiguous uninstall'
        )
    return installed[0] if installed else None


def _recover_interrupted_install() -> None:
    active = []
    for layout, mapping in (
        (directories(), targets()),
        (legacy_directories(), legacy_targets()),
    ):
        state = layout[2]
        if managed_exists(state / 'manifest.json') or managed_exists(state / 'transaction.json'):
            active.append((layout, mapping))
    if len(active) > 1:
        raise RuntimeError(
            'MULTIPLE_INSTALLATIONS: both legacy v0.8.0 and canonical v0.8.1+ states exist; '
            'refusing ambiguous uninstall'
        )
    if active:
        layout, mapping = active[0]
        InstallTransaction(layout[2]).recover(mapping)


def _begin_uninstall_cleanup(
    data: dict, manifest: Path, restored_content: dict[str, bytes | None]
) -> None:
    data['uninstall'] = {
        'status': 'cleanup',
        'restored_hashes': {
            dest: digest(content) if content is not None else None
            for dest, content in restored_content.items()
        },
    }
    atomic_write(manifest, (json.dumps(data, indent=2) + '\n').encode())
    uninstall_checkpoint('after_cleanup_prepare', manifest)


def _verify_uninstall_restored(data: dict, entries: list[dict]) -> None:
    try:
        uninstall_state = data['uninstall']
        hashes = uninstall_state['restored_hashes']
        destinations = {entry['dest'] for entry in entries}
        _require_manifest(isinstance(uninstall_state, dict), 'malformed uninstall state')
        _require_manifest(uninstall_state.get('status') == 'cleanup', 'malformed uninstall state')
        _require_manifest(isinstance(hashes, dict), 'malformed uninstall state')
        _require_manifest(set(hashes) == destinations, 'malformed uninstall state')
        _require_manifest(
            all(value is None or isinstance(value, str) for value in hashes.values()),
            'malformed uninstall state',
        )
        for entry in entries:
            dest = Path(entry['dest'])
            content = managed_read(dest, missing_ok=True)
            current_hash = digest(content) if content is not None else None
            if current_hash != hashes[entry['dest']]:
                raise RuntimeError(f'LOCAL_CHANGE: {dest}; restored uninstall target was modified')
    except (KeyError, TypeError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed uninstall cleanup state') from exc


def _uninstall_backup_paths(data: dict, backup_dir: Path) -> list[Path]:
    result = []
    for entry in rollback_entries(data):
        if not entry['existed']:
            continue
        backup_value = entry.get('backup')
        if not isinstance(backup_value, str):
            raise RuntimeError(  # noqa: TRY004 -- manifest errors keep the CLI contract.
                'INVALID_MANIFEST: existing target lacks backup path'
            )
        backup = Path(backup_value)
        expected_name = 'cli.json.bak' if entry is data.get('tui_config') else None
        if not _valid_backup_parent(backup, backup_dir) or not _valid_backup_name(
            backup, expected_name
        ):
            raise RuntimeError(f'INVALID_MANIFEST: unexpected backup path: {backup}')
        result.append(backup)
    return result


def _remove_verified_backups(data: dict, state: Path) -> None:
    backup_dir = state / 'backups'
    backups = _uninstall_backup_paths(data, backup_dir)
    for backup in backups:
        managed_unlink(backup, missing_ok=True)
        uninstall_checkpoint('after_backup_remove', backup)
    parents = {backup.parent for backup in backups if backup.parent != backup_dir}
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        _rmdir_if_empty(parent)
    _rmdir_if_empty(backup_dir)


def _prune_installed_directories(
    layout: tuple[Path, Path, Path], mapping: dict[Path, Path]
) -> None:
    _, data, _ = layout
    plugin_roots = {
        dest.parent for dest, source in mapping.items() if source.parent == ROOT / 'plugin'
    }
    candidates = set(plugin_roots)
    for dest in mapping:
        if dest.is_relative_to(data):
            parent = dest.parent
            while parent.is_relative_to(data):
                candidates.add(parent)
                if parent == data:
                    break
                parent = parent.parent
    for path in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        _rmdir_if_empty(path)


def _retained_backup_roots(state: Path) -> list[Path]:
    retained = []
    for path in (
        state / 'backups',
        state / 'backup-archives',
        state / 'transaction-backups',
    ):
        if _directory_is_nonempty(path):
            retained.append(path)
        else:
            _rmdir_if_empty(path)
    return retained


def _finish_uninstall(
    version: str,
    layout: tuple[Path, Path, Path],
    mapping: dict[Path, Path],
    data: dict,
    manifest: Path,
) -> None:
    state = layout[2]
    entries = rollback_entries(data)
    _verify_uninstall_restored(data, entries)
    _remove_verified_backups(data, state)
    _prune_installed_directories(layout, mapping)
    retained = _retained_backup_roots(state)
    uninstall_checkpoint('before_manifest_remove', manifest)
    durable_unlink(manifest)
    if data.get('rollback', {}).get('discard_unverified', False):
        print(f'UNINSTALLED: {version}; unverified originals discarded; restart OpenCode')
    else:
        print(f'UNINSTALLED: {version}; pre-installation files restored; restart OpenCode')
    for path in retained:
        print(f'RETAINED_BACKUPS: {path}; contents were not proven safe to delete')


def _uninstall_layout(
    version: str,
    layout: tuple[Path, Path, Path],
    mapping: dict[Path, Path],
    *,
    discard_unverified: bool = False,
) -> None:
    state = layout[2]
    manifest = state / 'manifest.json'
    manifest_content = managed_read(manifest)
    assert manifest_content is not None
    data = json.loads(manifest_content)
    validate_rollback_manifest(data, mapping)
    product_version = data.get('product_version')
    if (
        isinstance(product_version, str)
        and len(product_version) <= 32
        and product_version.count('.') == 2
        and all(part.isascii() and part.isdecimal() for part in product_version.split('.'))
    ):
        version = product_version
    entries = rollback_entries(data)
    if data.get('uninstall') is not None:
        _finish_uninstall(version, layout, mapping, data, manifest)
        return
    if (
        data.get('rollback') is not None
        and bool(data['rollback'].get('discard_unverified', False)) != discard_unverified
    ):
        raise RuntimeError('UNINSTALL_MODE_CHANGED: resume using the same --discard-backups option')
    if data.get('rollback') is None:
        check_managed(data, mapping)
    restored_content = _rollback_contents(
        data, state / 'backups', discard_unverified=discard_unverified
    )
    if data.get('rollback') is None:
        _begin_rollback(data, entries, manifest, discard_unverified=discard_unverified)
    else:
        _validate_rollback_hashes(data, entries)
    _restore_rollback_targets(data, entries, manifest, restored_content)
    _verify_rollback(entries, restored_content)
    _begin_uninstall_cleanup(data, manifest, restored_content)
    _finish_uninstall(version, layout, mapping, data, manifest)


def uninstall(*, discard_unverified: bool = False) -> None:
    with uninstall_locks():
        _recover_interrupted_install()
        selected = _select_uninstall_layout()
        if selected is None:
            print('NOT_INSTALLED')
            for state in (directories()[2], legacy_directories()[2]):
                for path in _retained_backup_roots(state):
                    print(
                        f'RETAINED_BACKUPS: {path}; no manifest proves the contents safe to delete'
                    )
            return
        _uninstall_layout(*selected, discard_unverified=discard_unverified)


def _argument_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description=(
            'Managed local Agentic Kit installer (no sudo, no network). '
            'Running without a mode installs by default.'
        )
    )
    group = cli.add_mutually_exclusive_group()
    group.add_argument(
        '--install',
        dest='mode',
        action='store_const',
        const='install',
        help='install (compatibility alias for the default mode)',
    )
    group.add_argument(
        '--dry-run',
        dest='mode',
        action='store_const',
        const='dry-run',
        help='show the installation plan without changing files',
    )
    group.add_argument(
        '--upgrade',
        dest='mode',
        action='store_const',
        const='upgrade',
        help='upgrade an existing managed installation after safety checks',
    )
    group.add_argument(
        '--rollback',
        dest='mode',
        action='store_const',
        const='rollback',
        help='recover or restore pre-installation files while retaining backups',
    )
    group.add_argument(
        '--uninstall',
        dest='mode',
        action='store_const',
        const='uninstall',
        help='recommended full removal of a v0.8.0 or v0.8.1+ installation',
    )
    cli.add_argument(
        '--discard-backups',
        action='store_true',
        help=(
            'with --uninstall, explicitly discard original files whose backups have no '
            'trusted hash; preserve unrelated current CLI settings'
        ),
    )
    cli.set_defaults(mode='install')
    return cli


def main():
    parser = _argument_parser()
    args = parser.parse_args()
    mode = args.mode
    if args.discard_backups and mode != 'uninstall':
        parser.error('--discard-backups requires --uninstall')
    actions = {
        'install': install,
        'upgrade': lambda: install(upgrade=True),
        'rollback': rollback,
        'uninstall': lambda: uninstall(discard_unverified=args.discard_backups),
        'dry-run': plan,
    }
    actions[mode]()


if __name__ == '__main__':
    main()
