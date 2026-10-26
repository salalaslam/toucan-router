import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


@pytest.mark.parametrize('package', ['bot', 'tools'])
def test_subpackages_have_the_root_mit_license(package):
    root = Path(__file__).resolve().parents[1]
    assert (root / package / 'LICENSE').read_bytes() == (root / 'LICENSE').read_bytes()


@pytest.fixture(scope='module')
def router_wheel(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path_factory.mktemp('router-wheel')
    backend_paths = []
    cache = Path(os.environ.get('UV_CACHE_DIR', '~/.cache/uv')).expanduser() / 'archive-v0'
    for module in ('hatchling', 'packaging', 'pathspec', 'pluggy', 'tomlkit', 'trove_classifiers'):
        if importlib.util.find_spec(module) is None:
            cached = sorted(cache.glob(f'*/{module}/__init__.py'))
            if not cached:
                pytest.skip(f'offline wheel build needs {module} installed or cached')
            backend_paths.append(str(cached[0].parents[1]))
    env = os.environ | dict(PYTHONPATH=os.pathsep.join(backend_paths), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c',
                             'import sys; from hatchling.build import build_wheel; print(build_wheel(sys.argv[1]))',
                             str(output)], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return output / result.stdout.strip()


def test_wheel_contains_reference_sources_units_and_example(router_wheel):
    root = Path(__file__).resolve().parents[1]
    sources = {'toucan/data/AGENTS.md': root / 'AGENTS.md',
               'toucan/data/skills/toucan-owner-thread/SKILL.md': root / 'skills/toucan-owner-thread/SKILL.md',
               'toucan/toucan.example.toml': root / 'examples/toucan.example.toml'}
    sources.update({f'toucan/data/docs/{path.name}': path for path in (root / 'docs').glob('*.md')})
    sources.update({f'toucan/data/systemd/{path.name}': path for path in (root / 'contrib/systemd').iterdir() if path.is_file()})
    with zipfile.ZipFile(router_wheel) as wheel:
        for destination, source in sources.items():
            assert wheel.read(destination) == source.read_bytes(), destination


def test_installed_wheel_guides_and_setup_prompt_work_without_checkout(router_wheel, tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = os.environ | dict(PYTHONPATH=str(router_wheel), PYTHONDONTWRITEBYTECODE='1', HOME=str(tmp_path),
                            TOUCAN_CONFIG=str(tmp_path / 'toucan.toml'), TOUCAN_DB=str(tmp_path / 'state/toucan.db'))

    def cli(*args):
        result = subprocess.run([sys.executable, '-S', '-m', 'toucan', *args], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout

    topics = json.loads(cli('guide', '--list', '--json'))
    assert topics == ['setup-t3', 'setup-whatsapp', 'setup-teams', 'setup-email', 'setup-upload',
                      'setup-sheets', 'setup-alerts', 'configuration', 'operations', 'owner-thread']
    assert cli('guide').rstrip() == (root / 'AGENTS.md').read_text().rstrip()
    for topic in topics:
        source = root / 'skills/toucan-owner-thread/SKILL.md' if topic == 'owner-thread' else root / f'docs/{topic}.md'
        assert cli('guide', topic).rstrip() == source.read_text().rstrip()
    assert json.loads(cli('init', '--owner-name', 'Alice', '--timezone', 'UTC', '--json'))['status'] == 'created'
    assert json.loads(cli('add', 'thread', 'project-b', '--json'))['status'] == 'updated'
    prompt = json.loads(cli('setup-prompt', 'project-b', '--json'))
    assert prompt['title'] == 'Toucan requests (project-b)'
    assert prompt['schedule'] == {'type': 'webhook'} and prompt['bindToCurrentThread'] is True
    assert prompt['hook_command'] == 'toucan hook set project-b'
    assert 'toucan inbox --thread project-b' in prompt['prompt'] and 'If Alice answers' in prompt['prompt']
    standby = json.loads(cli('setup-prompt', 'project-b', '--standby', '--json'))
    assert 'toucan log --request REQ' in standby['prompt'] and 'handed_over' in standby['prompt']
