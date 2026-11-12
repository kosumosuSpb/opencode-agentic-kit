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


def check_managed(manifest_data: dict, mapping: dict[Path, Path]) -> None:
    """An installed manifest must match our fixed target set and unedited bytes."""
    try:
        entries = manifest_data['files']
        if (not isinstance(entries, list) or len(entries) != len(mapping)
                or {e['dest'] for e in entries} != {str(d) for d in mapping}):
            raise RuntimeError('INVALID_MANIFEST: installed file list differs from current targets')
        for entry in entries:
            dest = Path(entry['dest'])
            if dest.is_symlink() or not dest.is_file() or digest(dest.read_bytes()) != entry['installed_hash']:
                raise RuntimeError(f'LOCAL_CHANGE: {dest}; refusing to overwrite local edits')
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('INVALID_MANIFEST: malformed preview installation state') from exc


def write_release(mapping: dict[Path, Path], content: dict[Path, bytes],
                  manifest: Path, entries: list[dict], prior_bytes: dict[Path, bytes]):
    """Apply a release; restore the currently installed version on an error."""
    touched = []
    try:
        for dest in mapping:
            if dest in prior_bytes and prior_bytes[dest] == content[dest]:
                continue
            touched.append(dest)
            atomic_write(dest, content[dest])
        data = {'version': VERSION, 'files': entries}
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
    if manifest.exists():
        prior = json.loads(manifest.read_text())
        check_managed(prior, mapping)
        if all(digest(content[dest]) == e['installed_hash'] for e in prior['files']
               for dest in [Path(e['dest'])]):
            print('ALREADY_INSTALLED: installed files match this source tree')
            return
        if not upgrade:
            raise RuntimeError('UPGRADE_REQUIRED: canonical sources changed; use --upgrade after review')
        # Original backups and existed flags stay untouched. --rollback must
        # restore the files from before the first preview install, not v1.2-preview.1.
        current = {dest: dest.read_bytes() for dest in mapping}
        entries = [dict(entry) for entry in prior['files']]
        for entry in entries:
            entry['source'] = str(mapping[Path(entry['dest'])])
            entry['installed_hash'] = digest(content[Path(entry['dest'])])
        write_release(mapping, content, manifest, entries, current)
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
    write_release(mapping, content, manifest, entries, previous)
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
    for entry in entries:
        if entry['existed'] and not Path(entry['backup']).is_file():
            raise RuntimeError(f'BACKUP_MISSING: {entry["backup"]}')
    for entry in entries:
        dest = Path(entry['dest'])
        if entry['existed']:
            atomic_write(dest, Path(entry['backup']).read_bytes())
        else:
            dest.unlink()
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
