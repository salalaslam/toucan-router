"""Small TOML serializer for Toucan configuration; comments are not preserved."""
import json
import math
import re


def literal(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return str(value).lower()
    if type(value) in (int, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return '[' + ', '.join(literal(v) for v in value) + ']'
    # Lists of string lists hold commands, such as settings.router_commands.
    if isinstance(value, list) and all(isinstance(v, list) and all(isinstance(a, str) for a in v) for v in value):
        return '[' + ', '.join(literal(v) for v in value) + ']'
    raise ValueError('unsupported TOML value; use text, numbers, booleans, string lists or lists of string lists')


def key(name):
    return name if re.fullmatch(r'[A-Za-z0-9_-]+', name) else literal(name)


def dumps(data):
    lines = []

    def table(entry, path=(), header=True):
        scalars = any(not isinstance(v, dict) and not (k == 'routes' and isinstance(v, list)) for k, v in entry.items())
        if path and header and (scalars or not entry):
            lines.extend(['', '[' + '.'.join(key(k) for k in path) + ']'])
        for name, value in entry.items():
            if not isinstance(value, dict) and not (name == 'routes' and value and isinstance(value, list)):
                lines.append(f'{key(name)} = {literal(value)}')
        for name, value in entry.items():
            if isinstance(value, dict):
                table(value, (*path, name))
        if not path and entry.get('routes'):
            for route in entry['routes']:
                lines.extend(['', '[[routes]]'])
                for name, value in route.items():
                    if name != 'active_ts':
                        lines.append(f'{key(name)} = {literal(value)}')

    table(data)
    return '\n'.join(lines).lstrip() + '\n'
