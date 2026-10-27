"""Transactional project-local ignore placement and lossless legacy migration."""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import adapters, gitignore_gate as gate, locking, manifest, protocol_json, transactions
from .sources import boundaries
from .sources.errors import SourceError


def owned_entries() -> list[str]:
    return adapters.all_gitignore_entries() + ['Skillfile.dev.json', '/.mcp.json', '/.codex/config.toml', '/.cursor/mcp.json']


def _regular_bytes(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise gate.GitignoreError(f'Cannot read ignore policy file: {path.name}') from exc
    if not stat.S_ISREG(mode) or not mode & 0o222:
        raise gate.GitignoreError(f'Ignore policy requires a writable regular file, never a symlink: {path.name}')
    try:
        return path.read_bytes()
    except OSError as exc:
        raise gate.GitignoreError(f'Cannot read ignore policy file: {path.name}') from exc


def remove_ignore_member(raw: bytes) -> bytes:
    """Delete just a top-level key/value and its separator; retain all other bytes."""
    try:
        data = protocol_json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('expected an object')
        text = raw.decode('utf-8')
        decoder = json.JSONDecoder()
        # Leading JSON whitespace is legal too.
        opening = len(text) - len(text.lstrip())
        i = opening + 1
        previous_comma: int | None = None
        while True:
            while text[i:i + 1].isspace():
                i += 1
            if text[i] == '}':
                return raw
            start = i
            key, i = decoder.raw_decode(text, i)
            while text[i].isspace():
                i += 1
            if text[i] != ':':
                raise ValueError('missing colon')
            i += 1
            while text[i].isspace():
                i += 1
            _, end = decoder.raw_decode(text, i)
            i = end
            while text[i].isspace():
                i += 1
            if key == 'ignore_mode':
                if text[i] == ',':
                    edited = text[:start] + text[end:i] + text[i + 1:]
                elif previous_comma is not None:
                    edited = text[:previous_comma] + text[previous_comma + 1:start] + text[end:]
                else:
                    edited = text[:start] + text[end:]
                # Guard the editor itself independently of its token spans.
                expected = dict(data)
                expected.pop('ignore_mode')
                result = edited.encode('utf-8')
                if protocol_json.loads(result) != expected:
                    raise ValueError('unsafe edit')
                return result
            if text[i] == '}':
                return raw
            previous_comma = i
            i += 1
    except (ValueError, IndexError, UnicodeError) as exc:
        raise SourceError('skillfile_migration_unsafe',
                          'Cannot safely remove ignore_mode; repair duplicate keys or JSON layout without reformatting unrelated data, then run csk ignore-mode set exclude (or csk ignore-mode set gitignore)') from exc


def _without_block(data: bytes) -> bytes:
    lines = data.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b'\r\n') == b'# CocoaSkill']
    ends = [i for i, line in enumerate(lines) if line.rstrip(b'\r\n') == b'# End CocoaSkill']
    if not starts and not ends:
        return data
    if len(starts) != 1 or len(ends) > 1 or (ends and ends[0] <= starts[0]):
        raise gate.GitignoreError('Ambiguous managed .gitignore block; repair its markers before retrying')
    start = starts[0]
    if ends:
        end = ends[0] + 1
        if any(line.rstrip(b'\r\n') not in {p.encode() for p in owned_entries()} for line in lines[start + 1:end - 1]):
            raise gate.GitignoreError('Unknown rule inside managed .gitignore block; move user rules outside the block before retrying')
    else:
        # Legacy blocks had no end marker. Only the contiguous known owned
        # rules are removable; every following byte remains user-owned.
        end = start + 1
        known = {p.encode() for p in owned_entries()}
        while end < len(lines) and lines[end].rstrip(b'\r\n') in known:
            end += 1
    return b''.join(lines[:start] + lines[end:])


def recheck_file(target: transactions.JournalTarget) -> None:
    payload = target.publication_recheck
    assert payload is not None
    root = Path(payload['project'])
    path = Path(target.live_path)
    if path.parent != root or path.name not in {'.gitignore', 'Skillfile.json'}:
        raise transactions.TransactionError('Invalid ignore-mode policy destination')
    info = root.stat()
    if (info.st_dev, info.st_ino) != boundaries._identity_from_json(
            payload['root_identity'], subject='Ignore policy project identity'):
        raise gate.GitignoreError('Ignore policy project identity changed')
    _regular_bytes(path)


def _mode_source(root: Path, mode: str, declared: str | None) -> str:
    if declared is not None:
        return 'deprecated_field'
    if mode == 'gitignore':
        return 'block'
    try:
        exclude = gate.ignore_path(root, 'exclude')
    except gate.ExcludeError:
        # A read-only show outside Git can still describe the default.
        return 'default'
    data = gate._exclude_bytes(exclude, writable=False)
    return 'block' if data and gate.project_block(data, gate.project_key(root)) is not None else 'default'


def show(root: Path) -> dict[str, Any]:
    loaded = manifest.load_manifest(root)
    declared = loaded.ignore_mode if loaded else None
    mode = gate.effective_mode(root, declared)
    return {'mode': mode, 'source': _mode_source(root, mode, declared),
            'deprecated_ignore_mode': declared}


def set_mode(root: Path, home: Path, mode: str, *, fault_hook: transactions.FaultHook | None = None) -> None:
    from .sources import publish

    if mode not in {'exclude', 'gitignore'}:
        raise ValueError('mode must be exclude or gitignore')
    root = root.resolve()
    exclude = gate.ignore_path(root, 'exclude')  # Existing outside-worktree refusal.
    with locking.ProjectLock(home, root), locking.ManagerHomeLock(home) as lock:
        engine = transactions.TransactionEngine(home, fault_hook=fault_hook, pre_write_hook=publish.make_publication_hook())
        engine.recover(lock)
        with gate.exclude_file_lock(exclude):
            ignore = root / '.gitignore'
            skillfile = root / manifest.MANIFEST_NAME
            root_info = root.stat()
            old_ignore = _regular_bytes(ignore)
            raw = _regular_bytes(skillfile)
            preimages = {path: transactions.digest_target(path) for path in (ignore, skillfile)}
            if _regular_bytes(ignore) != old_ignore or _regular_bytes(skillfile) != raw:
                raise gate.GitignoreError('Ignore policy input changed while being read')
            original = gate._exclude_bytes(exclude)
            # Validate duplicate keys before loading, and preserve malformed
            # input rather than falling back to a whole-document serializer.
            edited = remove_ignore_member(raw) if raw is not None else None
            loaded = manifest.load_manifest(root)
            if loaded is None:
                raise manifest.ManifestError('Skillfile.json missing; run csk init first')
            remaining = _without_block(old_ignore or b'')
            entries = owned_entries()
            for entry in entries:
                if gate.is_tracked(root, entry.lstrip('/').rstrip('/')):
                    raise gate.GitignoreError(f'Owned path is already tracked: {entry}')
            desired_ignore: bytes | None
            if mode == 'gitignore':
                managed = b'# CocoaSkill\n' + b'\n'.join(p.encode() for p in entries) + b'\n# End CocoaSkill\n'
                # A prefix avoids inventing an unremovable separator byte in
                # a user file with no final newline. Git still verifies it.
                desired_ignore = (managed + remaining if remaining and not remaining.endswith(b'\n')
                                  else remaining + managed)
            else:
                desired_ignore = remaining if old_ignore is not None else None
            key = gate.project_key(root)
            old_block = gate.project_block(original, key)
            if mode == 'exclude':
                proposed = gate.plan_ignored(root, entries, fix=True, mode='exclude')
                block = gate.project_block(original if proposed is None else proposed, key)
            else:
                block = None
            desired_exclude = gate.replace_project_block(original, key, block)
            with tempfile.TemporaryDirectory(prefix='ignore-mode-', dir=home) as tmp:
                staging = Path(tmp)
                gate.verify_planned_ignored(root, entries, desired_exclude, staging,
                                           mode='exclude', project_ignore=desired_ignore or b'')
                if any(transactions.digest_target(path) != digest for path, digest in preimages.items()):
                    raise gate.GitignoreError('Ignore policy input changed during planning; retry the mode switch')
                targets = []
                for path, before, after in [(ignore, old_ignore, desired_ignore), (skillfile, raw, edited)]:
                    if before == after:
                        continue
                    desired = staging / path.name if after is not None else None
                    if desired is not None:
                        assert after is not None
                        desired.write_bytes(after)
                    targets.append(transactions.MutableTarget(
                        'ignore-mode-file', path.name, path, desired,
                        expected_preimage_digest=preimages[path],
                        publication_recheck={'kind': 'ignore-mode-file', 'project': str(root),
                                             'root_identity': boundaries._identity_to_json((root_info.st_dev, root_info.st_ino))}))
                # Include the shared slice even for a no-op: recovery must lock
                # it while restoring the other policy preimages.
                desired_block = staging / 'exclude-block' if block is not None else None
                if desired_block is not None:
                    assert block is not None
                    desired_block.write_bytes(block)
                common = exclude.parent.parent
                common_info = common.stat()
                exclude.parent.mkdir(exist_ok=True)
                ancestor = exclude.parent.lstat()
                expected = transactions.shared_block_digest(old_block)
                targets.append(transactions.MutableTarget(
                    'mcp-gitignore', 'ignore-mode-exclude', exclude, desired_block,
                    expected_preimage_digest=expected,
                    publication_recheck={
                        'kind': 'git-common-exclude-block', 'project': str(root), 'block_key': key,
                        'old_block': base64.b64encode(old_block).decode('ascii') if old_block else None,
                        'file_existed': exclude.exists(), 'original_sha256': hashlib.sha256(original).hexdigest(),
                        'root': str(common), 'root_identity': publish._identity_pairs_to_json([(common_info.st_dev, common_info.st_ino)]),
                        'ancestors': publish._identity_pairs_to_json([(ancestor.st_dev, ancestor.st_ino)]),
                        'expected_live': expected, 'ignore_mode_switch': True}))
                plan = transactions.TransactionPlan('ignore-mode-' + uuid.uuid4().hex,
                                                    locking.canonical_project_identity(root), tuple(targets))
                engine.prepare(lock, plan)
                engine.commit(lock, plan.transaction_id)
