"""Agent setup commands and bundled reference material."""
import os
import stat
import sys
from importlib.resources import files
from pathlib import Path

from . import config, configedit, sources, t3
from .storage import private_dir, state_path

TOPICS = ('setup-t3', 'setup-whatsapp', 'setup-teams', 'setup-email', 'setup-upload',
          'setup-sheets', 'setup-alerts', 'configuration', 'operations', 'owner-thread')


def hooks_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'toucan/hooks'


def resource(relative):
    bundled = files('toucan').joinpath('data', *relative.split('/'))
    if bundled.is_file():
        return bundled.read_text()
    path = Path(__file__).resolve().parents[2] / relative
    return path.read_text()


def guide(topic=None, listing=False):
    if listing:
        return list(TOPICS)
    if topic is None:
        return resource('AGENTS.md')
    if topic not in TOPICS:
        raise config.ValidationError('unknown guide topic; use toucan guide --list')
    return resource('skills/toucan-owner-thread/SKILL.md' if topic == 'owner-thread' else f'docs/{topic}.md')


def initialize(path, force=False, example=False, owner_name=None, timezone=None, owner_whatsapp=None, owner_teams_id=None):
    if example:
        import tomllib
        example_text = files('toucan').joinpath('toucan.example.toml').read_text()
        raw = tomllib.loads(example_text)
    else:
        if not owner_name or not owner_name.strip() or not timezone:
            raise config.ValidationError('init requires --owner-name and --timezone (or --example)')
        raw = dict(settings=dict(owner_name=owner_name, timezone=timezone), sources={}, t3={}, people={}, threads={}, routes=[])
    for key, value in dict(owner_name=owner_name, timezone=timezone, owner_whatsapp=owner_whatsapp, owner_teams_id=owner_teams_id).items():
        if value is not None:
            raw['settings'][key] = value
    configedit.validate(raw)
    if path.exists() and not force:
        try:
            same = configedit.read(path) == raw
        except config.ValidationError:
            same = False
        if not same:
            raise config.ValidationError('config already exists with different values; use --force to overwrite')
    plain_example = example and all(v is None for v in (owner_name, timezone, owner_whatsapp, owner_teams_id))
    status = configedit.save(path, raw, text=example_text if plain_example else None)
    private_dir(hooks_dir())
    private_dir(state_path().parent)
    return dict(status=status, message='Config ' + status + '. Run toucan doctor.')


def thread(cfg, key):
    if key not in cfg['threads']:
        raise config.ValidationError('unknown thread')
    return cfg['threads'][key]


def hook(action, key, cfg, paths=None):
    entry = thread(cfg, key)
    path = Path(entry['hook_file']).expanduser()
    if action == 'set':
        url = sys.stdin.read().strip()
        if not config.http_url(url) or not t3.hook_info(url):
            raise config.ValidationError('invalid webhook URL; expected HTTP(S) /api/hooks/TASK/TOKEN')
        same = path.is_file() and path.read_text().strip() == url and stat.S_IMODE(path.stat().st_mode) == 0o600
        if not same:
            configedit.atomic(path, url + '\n')
        return dict(status='unchanged' if same else 'updated', thread=key)
    warning = config.hook_warning(entry)
    resolved = sources.hook_file_thread(paths or sources.Paths.from_config(cfg), entry['hook_file'])
    # A hook check always redacts the resolved thread, including JSON.
    return dict(status='invalid' if warning else 'ok', thread=key, resolved_thread=('…' + resolved[-4:]) if resolved else None)


def setup_prompt(cfg, key, standby=False):
    thread(cfg, key)
    text = resource('docs/setup-t3.md')
    prompt = text.split('```text\n', 1)[1].split('\n```', 1)[0].replace('project-a', key)
    prompt = prompt.replace('If the owner answers', f'If {cfg["settings"]["owner_name"]} answers')
    if standby:
        prompt += ('\nYou are the fallback thread on another provider. Read `toucan log --request REQ` '
                   '(the `handed_over` event names the original thread) and work in that project\'s checkout.')
    return dict(title=f'Toucan requests ({key})', schedule=dict(type='webhook'), bindToCurrentThread=True,
                prompt=prompt, hook_command=f'toucan hook set {key}')


def safe(value, show_ids=False, key=''):
    """Display config without secrets, and human output without identity values."""
    if key in ('alert_url', 'ntfy'):
        return '[redacted]' if value else value
    if isinstance(value, dict):
        return {k: safe(v, show_ids, k) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v, show_ids, key) for v in value]
    if isinstance(value, str):
        if '/api/hooks/' in value or '://' in value:
            return '[redacted]'
        if not show_ids and key in ('owner_whatsapp', 'owner_teams_id', 'whatsapp', 'teams_id', 'email', 'chats', 'jid', 'lid_aliases', 'conversation_id', 'aad_id', 'address', 'thread_id', 't3_thread'):
            return '…' + value[-4:] if value else value
    return value
