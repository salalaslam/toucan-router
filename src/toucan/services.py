"""Install only unmodified Toucan user units; all systemd effects are injected."""
import os
import re
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

from . import configedit, setup
from .config import load
from .effects import Effects, local_run

MANAGED = '# Managed by toucan enable\n'
UNITS = ('toucan-sync.service', 'toucan-sync.timer', 'wacli-sync.service', 'toucan-bot.service')


def directory():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser() / 'systemd/user'


def template(name):
    resource = files('toucan').joinpath('data/systemd', name)
    if resource.is_file():
        return resource.read_text()
    return (Path(__file__).resolve().parents[2] / 'contrib/systemd' / name).read_text()


def command_path(name):
    value = shutil.which(name)
    return str(Path(value).absolute()) if value else None


def quote(value, environment=False):
    # systemd command lines use their own quoting and expand percent specifiers.
    text = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r')
    return '"' + (text if environment else text.replace('$', '$$')) + '"'


def content(name, executable=None, config_path=None, cfg=None):
    text = template(name)
    if executable:
        text = re.sub(r'^ExecStart=\S+(.*)$', lambda m: 'ExecStart=' + quote(executable) + m[1], text, flags=re.M)
    if name == 'toucan-sync.service':
        environment = 'Environment=' + quote('TOUCAN_CONFIG=' + str(config_path.absolute()), environment=True) + '\n'
        if os.environ.get('TOUCAN_DB'):
            environment += 'Environment=' + quote('TOUCAN_DB=' + str(Path(os.environ['TOUCAN_DB']).expanduser().absolute()), environment=True) + '\n'
        environment += 'Environment=' + quote('PATH=' + os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), environment=True) + '\n'
        text = re.sub(r'^Environment=PATH=.*\n', '', text, flags=re.M)
        text = text.replace('[Service]\n', '[Service]\n' + environment)
    if name == 'wacli-sync.service':
        store = str(Path(cfg['sources']['whatsapp']['store']).expanduser().absolute())
        text = text.replace(' sync --follow', ' --store ' + quote(store) + ' sync --follow')
    return MANAGED + text


def run(fx, argv, required=True):
    try:
        result = local_run(fx, argv, timeout=5)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        if required:
            raise RuntimeError('systemd command failed or timed out') from None
        return None
    if required and result.returncode:
        raise RuntimeError('systemd command failed; check your user systemd session')
    return result


def systemctl(fx, *args, required=True):
    return run(fx, ['systemctl', '--user', *args], required)


def state(fx, verb, name):
    result = systemctl(fx, verb, name, required=False)
    accepted = ('enabled', 'enabled-runtime') if verb == 'is-enabled' else ('active',)
    return bool(result and result.returncode == 0 and result.stdout.strip() in accepted)


def active_whatsapp(fx):
    result = systemctl(fx, 'list-units', '--type=service', '--state=active', '--no-legend', '--plain')
    names = [line.split()[0] for line in result.stdout.splitlines() if line.split() and line.split()[0].endswith('.service')]
    for name in names:
        if name == 'wacli-sync.service':
            return name
        unit = systemctl(fx, 'show', name, '--property=ExecStart', '--value')
        if re.search(r'(?:^|[/\s{;])wacli(?:\s|$).*\bsync\b', unit.stdout):
            return name
    return None


def enable(path=None, effects=None, paths=None, dry_run=False):
    path, fx = setup.config_path(path), effects or Effects()
    cfg, raw = load(path), configedit.read(path)
    target = directory()
    if not shutil.which('systemctl'):
        raise RuntimeError('systemctl unavailable; user units require systemd')
    executable = command_path('toucan')
    if not executable:
        raise RuntimeError('toucan is not on PATH; install toucan-router first')
    selected = {'toucan-sync.service': executable, 'toucan-sync.timer': None}
    skipped = []
    active = {r['source'] for r in cfg['routes']}
    if 'whatsapp' in active or cfg['settings']['owner_whatsapp']:
        wacli = command_path('wacli')
        existing = active_whatsapp(fx) if wacli else None
        if existing:
            skipped.append(dict(name='wacli-sync.service', status='skipped', reason=f'WhatsApp sync already active in {existing}'))
        elif wacli:
            selected['wacli-sync.service'] = wacli
        else:
            skipped.append(dict(name='wacli-sync.service', status='skipped', reason='wacli is not on PATH'))
    if 'teams' in active:
        bot = command_path('toucan-bot')
        if bot:
            selected['toucan-bot.service'] = bot
        else:
            skipped.append(dict(name='toucan-bot.service', status='skipped', reason='toucan-bot is not on PATH'))
    units, texts = [], {}
    for name, executable in selected.items():
        output, unit_path = content(name, executable, path, cfg), target / name
        exists = unit_path.exists() or unit_path.is_symlink()
        same = exists and not unit_path.is_symlink() and unit_path.read_text() == output
        refused = exists and not same
        units.append(dict(name=name, path=str(unit_path), status='refused' if refused else 'unchanged' if same else 'planned',
                          **({'reason': 'existing unit differs; left untouched'} if refused else {}),
                          **({'content': output} if dry_run else {})))
        if not refused:
            texts[name] = output
    if 'toucan-sync.service' not in texts:
        texts.pop('toucan-sync.timer', None)
        timer = next(u for u in units if u['name'] == 'toucan-sync.timer')
        timer.update(status='refused', reason='toucan-sync.service was refused; timer left untouched')
    for source, name in [('whatsapp', 'wacli-sync.service'), ('teams', 'toucan-bot.service')]:
        if name in texts:
            raw.setdefault('sources', {}).setdefault(source, {})['service_unit'] = name
    configedit.validate(raw)
    actions = []
    changed_files = [u for u in units if u['status'] == 'planned' and u['name'] in texts]
    if changed_files:
        actions.append(['systemctl', '--user', 'daemon-reload'])
    for name in texts:
        if name == 'toucan-sync.service':
            continue
        if not state(fx, 'is-enabled', name) or not state(fx, 'is-active', name):
            actions.append(['systemctl', '--user', 'enable', '--now', name])
    refused = any(u['status'] == 'refused' for u in units)
    result = dict(status='refused' if refused else 'dry-run' if dry_run else 'unchanged',
                  directory=str(target), units=units + skipped, actions=actions, doctor=[])
    if dry_run:
        return result
    config_status = configedit.save(path, raw)
    for unit in changed_files:
        configedit.atomic(Path(unit['path']), texts[unit['name']])
        unit['status'] = 'installed'
    for argv in actions:
        run(fx, argv)
    if not refused and (changed_files or actions or config_status != 'unchanged'):
        result['status'] = 'updated'
    result['doctor'] = setup.doctor(path, fx, paths)
    return result


def disable(path=None, effects=None, paths=None):
    path, fx = setup.config_path(path), effects or Effects()
    load(path)  # No effects when the complete config is invalid.
    target, units, actions = directory(), [], []
    if not shutil.which('systemctl'):
        raise RuntimeError('systemctl unavailable; user units require systemd')
    for name in UNITS:
        unit_path = target / name
        if not unit_path.exists() and not unit_path.is_symlink():
            continue
        if unit_path.is_symlink() or not unit_path.read_text().startswith(MANAGED):
            units.append(dict(name=name, path=str(unit_path), status='refused', reason='unit is not managed by toucan enable'))
            continue
        units.append(dict(name=name, path=str(unit_path), status='unchanged'))
        if name == 'toucan-sync.service':
            if state(fx, 'is-active', name):
                actions.append(['systemctl', '--user', 'stop', name])
        elif state(fx, 'is-enabled', name) or state(fx, 'is-active', name):
            actions.append(['systemctl', '--user', 'disable', '--now', name])
    for argv in actions:
        run(fx, argv)
    for unit in units:
        if any(unit['name'] in argv for argv in actions):
            unit['status'] = 'disabled'
    return dict(status='refused' if any(u['status'] == 'refused' for u in units) else 'updated' if actions else 'unchanged',
                directory=str(target), units=units, actions=actions)
