#!/usr/bin/env python3
"""Opt-in OpenCode Agentic Kit installer from this source tree. No network or sudo."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent
VERSION = '1.2'
LEGACY_TUI_PLUGIN_SPEC = './plugins/agentic-preview/tui.ts'


def directories():
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
    data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
    state = Path(os.environ.get('XDG_STATE_HOME', home / '.local/state'))
    # The on-disk release name stays 'preview' for compatibility with the
    # currently installed v1.2-preview.1 plugin and existing rollback state.
    return config / 'opencode', data / 'opencode-agentic/preview', state / 'opencode-agentic/preview-install'


def targets():
    config, data, _ = directories()
    return {
        config / 'plugins/agentic-preview/index.ts': ROOT / 'plugin/index.ts',
        config / 'plugins/agentic-preview/tui.ts': ROOT / 'plugin/tui.ts',
        config / 'plugins/agentic-preview/package.json': ROOT / 'plugin/package.json',
        config / 'agents/architect.md': ROOT / 'agents/architect.md',
        config / 'agents/developer.md': ROOT / 'agents/developer.md',
        config / 'agents/reviewer.md': ROOT / 'agents/reviewer.md',
        data / 'runner/agentic_gate.py': ROOT / 'runner/agentic_gate.py',
    }


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(dest: Path, content: bytes):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


def plan():
    mapping = targets()
    for dest, source in mapping.items():
        if dest.is_symlink():
            status = 'SYMLINK CONFLICT'
        elif not dest.exists():
            status = 'CREATE'
        elif dest.is_file() and dest.read_bytes() == source.read_bytes():
            status = 'UNCHANGED'
        else:
            status = 'BACKUP+REPLACE'
        print(f'{status:16s} {dest}')
    tui_dest, tui_original, tui_content = tui_config_patch()
    tui_status = 'UNCHANGED' if tui_original == tui_content else ('CREATE' if tui_original is None else 'PATCH')
    print(f'{tui_status:16s} {tui_dest} ({tui_plugin_spec()})')
    _, _, state = directories()
    manifest = state / 'manifest.json'
    if manifest.exists():
        print('Existing managed installation detected; changed sources require explicit --upgrade.')
    print(f'Backup state: {state}')
    print(f'Source tree: {ROOT}')
    print('Preview conflicts with the lab plugin if both are active in the lab project.')


def source_bytes(mapping: dict[Path, Path]) -> dict[Path, bytes]:
    """Read all source files before touching any installation target."""
    content = {}
    for dest, source in mapping.items():
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f'INVALID_SOURCE: {source}')
        content[dest] = source.read_bytes()
    return content


def tui_config_patch() -> tuple[Path, bytes | None, bytes]:
    config, _, _ = directories()
    dest = config / 'cli.json'
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        raise RuntimeError(f'Found symlink or non-file target; refusing to overwrite: {dest}')
    original = dest.read_bytes() if dest.exists() else None
    try:
        data = json.loads(original.decode()) if original is not None else {
            '$schema': 'https://opencode.ai/v2/cli.json',
        }
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'INVALID_TUI_CONFIG: {dest} must be valid JSON') from exc
    if not isinstance(data, dict):
        raise RuntimeError(f'INVALID_TUI_CONFIG: {dest} must contain a JSON object')
    plugins = data.setdefault('plugins', [])
    if not isinstance(plugins, list) or not all(
        isinstance(item, (str, list))
        or (isinstance(item, dict) and isinstance(item.get('package'), str))
        for item in plugins
    ):
        raise RuntimeError(f'INVALID_TUI_CONFIG: {dest} plugins must be a list of package directives')
    spec = tui_plugin_spec()
    plugins[:] = [item for item in plugins if item != LEGACY_TUI_PLUGIN_SPEC]
    if spec not in plugins:
        plugins.append(spec)
    return dest, original, (json.dumps(data, indent=2) + '\n').encode()


def tui_plugin_spec() -> str:
    config, _, _ = directories()
    return str(config / 'plugins/agentic-preview/tui.ts')


def check_managed(manifest_data: dict, mapping: dict[Path, Path]) -> None:
    """A manifest may be an older target subset, but never contain unknown/edited files."""
    try:
        entries = manifest_data['files']
        destinations = [e['dest'] for e in entries]
        if (not isinstance(entries, list) or len(destinations) != len(set(destinations))
                or not set(destinations) <= {str(d) for d in mapping}):
            raise RuntimeError('INVALID_MANIFEST: installed file list is not a known target subset')
        for entry in entries:
            dest = Path(entry['dest'])
            if dest.is_symlink() or not dest.is_file() or digest(dest.read_bytes()) != entry['installed_hash']:
                raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite local edits')
        tui = manifest_data.get('tui_config')
        if tui is not None:
            dest = Path(tui['dest'])
            expected = directories()[0] / 'cli.json'
            if dest != expected or dest.is_symlink() or not dest.is_file():
                raise RuntimeError('INVALID_MANIFEST: invalid TUI config target')
            if digest(dest.read_bytes()) != tui['installed_hash']:
                raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite local edits')
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed preview installation state') from exc


def write_release(mapping: dict[Path, Path], content: dict[Path, bytes],
                  manifest: Path, entries: list[dict], prior_bytes: dict[Path, bytes],
                  tui_entry: dict):
    """Apply a release; restore the currently installed version on an error."""
    touched = []
    try:
        for dest in mapping:
            if dest in prior_bytes and prior_bytes[dest] == content[dest]:
                continue
            touched.append(dest)
            atomic_write(dest, content[dest])
        data = {'version': VERSION, 'files': entries, 'tui_config': tui_entry}
        atomic_write(manifest, (json.dumps(data, indent=2) + '\n').encode())
    except Exception:
        for dest in reversed(touched):
            if dest in prior_bytes:
                atomic_write(dest, prior_bytes[dest])
            else:
                dest.unlink(missing_ok=True)
        raise


def install(*, upgrade: bool = False):
    mapping = targets()
    content = source_bytes(mapping)
    _, _, state = directories()
    manifest = state / 'manifest.json'
    tui_dest, tui_original, tui_content = tui_config_patch()
    if manifest.exists():
        prior = json.loads(manifest.read_text())
        check_managed(prior, mapping)
        if (len(prior['files']) == len(mapping) and prior.get('tui_config') is not None
                and all(digest(content[dest]) == e['installed_hash'] for e in prior['files']
                        for dest in [Path(e['dest'])])
                and tui_dest.read_bytes() == tui_content):
            print('ALREADY_INSTALLED: installed files match this source tree')
            return
        if not upgrade:
            raise RuntimeError('UPGRADE_REQUIRED: canonical sources changed; use --upgrade after review')
        # Original backups and existed flags stay untouched. --rollback must
        # restore the files from before the first preview install, not v1.2-preview.1.
        if any(dest.is_symlink() or (dest.exists() and not dest.is_file()) for dest in mapping):
            raise RuntimeError('Found symlink or non-file target; refusing to overwrite')
        current = {dest: dest.read_bytes() for dest in mapping if dest.exists()}
        entries = [dict(entry) for entry in prior['files']]
        installed = {Path(entry['dest']) for entry in entries}
        backups = state / 'backups'
        backups.mkdir(exist_ok=True)
        tui_entry = prior.get('tui_config')
        if tui_entry is None:
            tui_backup = backups / 'cli.json.bak' if tui_original is not None else None
            if tui_backup:
                atomic_write(tui_backup, tui_original)
            tui_entry = {'dest': str(tui_dest), 'existed': tui_original is not None,
                         'backup': str(tui_backup) if tui_backup else None}
        for dest, source in mapping.items():
            if dest in installed:
                continue
            original = current.get(dest)
            saved = backups / f'{len(entries)}.bak' if original is not None else None
            if saved:
                atomic_write(saved, original)
            entries.append({'dest': str(dest), 'source': str(source),
                            'existed': original is not None,
                            'backup': str(saved) if saved else None,
                            'installed_hash': digest(content[dest])})
        for entry in entries:
            entry['source'] = str(mapping[Path(entry['dest'])])
            entry['installed_hash'] = digest(content[Path(entry['dest'])])
        tui_entry['installed_hash'] = digest(tui_content)
        if tui_original != tui_content:
            atomic_write(tui_dest, tui_content)
        try:
            write_release(mapping, content, manifest, entries, current, tui_entry)
        except Exception:
            if tui_original is None:
                tui_dest.unlink(missing_ok=True)
            else:
                atomic_write(tui_dest, tui_original)
            raise
        print(f'UPGRADED: {VERSION}. Original rollback backups preserved; restart OpenCode.')
        return
    if upgrade:
        raise RuntimeError('NOT_INSTALLED: --upgrade requires an existing preview; use --install')
    if any(dest.is_symlink() or (dest.exists() and not dest.is_file()) for dest in mapping):
        raise RuntimeError('Found symlink or non-file target; refusing to overwrite')
    state.mkdir(parents=True, exist_ok=True)
    backups = state / 'backups'
    backups.mkdir(exist_ok=True)
    entries = []
    previous = {}
    for i, (dest, source) in enumerate(mapping.items()):
        original = dest.read_bytes() if dest.exists() else None
        saved = backups / f'{i}.bak' if original is not None else None
        if saved:
            atomic_write(saved, original)
            previous[dest] = original
        entries.append({'dest': str(dest), 'source': str(source), 'existed': original is not None,
                        'backup': str(saved) if saved else None, 'installed_hash': digest(content[dest])})
    tui_backup = backups / 'cli.json.bak' if tui_original is not None else None
    if tui_backup:
        atomic_write(tui_backup, tui_original)
    tui_entry = {'dest': str(tui_dest), 'existed': tui_original is not None,
                 'backup': str(tui_backup) if tui_backup else None,
                 'installed_hash': digest(tui_content)}
    atomic_write(tui_dest, tui_content)
    try:
        write_release(mapping, content, manifest, entries, previous, tui_entry)
    except Exception:
        if tui_original is None:
            tui_dest.unlink(missing_ok=True)
        else:
            atomic_write(tui_dest, tui_original)
        raise
    print(f'INSTALLED: {VERSION}. Restart OpenCode. Run install.py --rollback to restore prior files.')


def rollback():
    _, _, state = directories()
    manifest = state / 'manifest.json'
    if not manifest.exists():
        print('NOT_INSTALLED')
        return
    data = json.loads(manifest.read_text())
    mapping = targets()
    check_managed(data, mapping)
    entries = data['files']
    tui = data.get('tui_config')
    if tui and tui['existed'] and not Path(tui['backup']).is_file():
        raise RuntimeError(f'BACKUP_MISSING: {tui["backup"]}')
    for entry in entries:
        if entry['existed'] and not Path(entry['backup']).is_file():
            raise RuntimeError(f'BACKUP_MISSING: {entry["backup"]}')
    for entry in entries:
        dest = Path(entry['dest'])
        if entry['existed']:
            atomic_write(dest, Path(entry['backup']).read_bytes())
        else:
            dest.unlink()
    if tui:
        tui_dest = Path(tui['dest'])
        if tui['existed']:
            atomic_write(tui_dest, Path(tui['backup']).read_bytes())
        else:
            tui_dest.unlink()
    manifest.unlink()
    print('ROLLED_BACK: pre-preview files restored; restart OpenCode')


def main():
    cli = argparse.ArgumentParser(description='Managed local Agentic Kit installer (no sudo, no network)')
    group = cli.add_mutually_exclusive_group()
    group.add_argument('--install', action='store_true')
    group.add_argument('--upgrade', action='store_true')
    group.add_argument('--rollback', action='store_true')
    args = cli.parse_args()
    if args.install:
        install()
    elif args.upgrade:
        install(upgrade=True)
    elif args.rollback:
        rollback()
    else:
        plan()


if __name__ == '__main__':
    main()
