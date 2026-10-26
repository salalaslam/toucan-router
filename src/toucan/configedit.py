"""Validated, atomic configuration edits. Keep raw data separate from loaded defaults."""
import copy
import os
import re
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, tomlw
from .storage import private_dir


def read(path):
    try:
        with Path(path).open('rb') as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise config.ValidationError('cannot load config; check its path and TOML syntax') from None


def atomic(path, content):
    path = Path(path).expanduser()
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb' if isinstance(content, bytes) else 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate(raw):
    try:
        text = tomlw.dumps(raw)
        cfg = config.validate(copy.deepcopy(tomllib.loads(text)))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, config.ValidationError):
            raise
        raise config.ValidationError('unsupported configuration value') from None
    unknown = [w for w in config.warnings(cfg) if w.startswith('unknown key ')]
    if unknown:
        raise config.ValidationError('; '.join(unknown))
    return text


def save(path, raw, *, text=None):
    path = Path(path).expanduser()
    serialized = validate(raw)
    text = serialized if text is None else text
    if tomllib.loads(text) != raw:
        raise config.ValidationError('serialized config differs from candidate')
    try:
        same = path.exists() and read(path) == raw
    except config.ValidationError:
        same = False
    if same:
        return 'unchanged'
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix='.toucan-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        config.load(name)
        existed = path.exists()
        if existed:
            atomic(path.with_name(path.name + '.bak'), path.read_bytes())
        os.replace(name, path)
        return 'updated' if existed else 'created'
    finally:
        if os.path.exists(name):
            os.unlink(name)


def key_name(key):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise config.ValidationError('keys must contain only letters, digits, underscores and hyphens')
    return key


def locate(raw, key, create=False):
    parts = key.split('.')
    if not parts or parts[0] not in config.KEYS:
        raise config.ValidationError('unknown configuration key')
    item = raw
    for index, part in enumerate(parts[:-1]):
        if isinstance(item, list):
            item = next((r for r in item if r.get('name') == part), None)
        elif isinstance(item, dict):
            if create and part not in item and not (index == 0 and part == 'routes'):
                item[part] = {}
            item = item.get(part)
        else:
            item = None
        if item is None:
            raise config.ValidationError('configuration key does not exist')
    if isinstance(item, list):
        found = next((r for r in item if r.get('name') == parts[-1]), None)
        if found is None:
            raise config.ValidationError('configuration key does not exist')
        return item, item.index(found)
    if not isinstance(item, dict):
        raise config.ValidationError('configuration key is not a table')
    return item, parts[-1]


def get(raw, key=None):
    if not key:
        return raw
    item, name = locate(raw, key)
    try:
        return item[name]
    except KeyError:
        raise config.ValidationError('configuration key does not exist') from None


def value(text):
    try:
        return tomllib.loads('value = ' + text)['value']
    except (tomllib.TOMLDecodeError, KeyError):
        return text


def edit(path, action, key, values=None, list_values=False):
    raw = read(path)
    # Read defaults for get only; edits always preserve raw data and secret file references.
    if action == 'get':
        return dict(key=key, value=get(config.load(path), key))
    if key.split('.')[-1] == 'active_ts':
        raise config.ValidationError('active_ts is computed; set active_from instead')
    if action == 'unset':
        try:
            get(raw, key)
        except config.ValidationError:
            validate(raw)
            return dict(status='unchanged', key=key)
    item, name = locate(raw, key, create=action == 'set')
    if action == 'set':
        if values is None:
            raise config.ValidationError('set requires VALUE or --value')
        item[name] = values if list_values else value(values[0])
    elif isinstance(item, list):
        item.pop(name)
    else:
        item.pop(name, None)
    return dict(status=save(path, raw), key=key)


def add(path, kind, key, entry, replace=False, now=None):
    key_name(key)
    raw = read(path)
    table = dict(person='people', thread='threads', route='routes')[kind]
    target = raw.setdefault(table, [] if kind == 'route' else {})
    old = next((r for r in target if r['name'] == key), None) if kind == 'route' else target.get(key)
    if kind == 'route':
        entry['name'] = key
        if 'active_from' not in entry:
            entry['active_from'] = old['active_from'] if old else datetime.fromtimestamp(now, ZoneInfo(config.load(path)['settings']['timezone'])).isoformat()
    if old is not None and old != entry and not replace:
        raise config.Refused('entry already exists with different values; use --replace')
    if kind == 'route':
        if old is None:
            target.append(entry)
        else:
            target[target.index(old)] = entry
    else:
        target[key] = entry
    return dict(status=save(path, raw), kind=kind, key=key)


def remove(path, kind, key):
    raw = read(path)
    refs = []
    for route in raw.get('routes', []):
        if (kind == 'person' and (key in route['requesters'] or route.get('reply_to') == key) or
                kind == 'thread' and route['thread'] == key):
            refs.append('routes.' + route['name'])
    if kind == 'thread':
        refs += ['threads.' + k + '.fallback' for k, t in raw.get('threads', {}).items() if t.get('fallback') == key]
    if refs:
        raise config.Refused('still referenced by ' + ', '.join(refs))
    table = dict(person='people', thread='threads', route='routes')[kind]
    exists = any(r['name'] == key for r in raw.get(table, [])) if kind == 'route' else key in raw.get(table, {})
    if not exists:
        validate(raw)
        return dict(status='unchanged', kind=kind, key=key)
    if kind == 'route':
        raw[table] = [r for r in raw.get(table, []) if r['name'] != key]
    else:
        raw.get(table, {}).pop(key, None)
    return dict(status=save(path, raw), kind=kind, key=key)
