"""The clock, subprocesses and HTTP are injected at the application boundary."""
import subprocess
import time
import urllib.request
import inspect
from dataclasses import dataclass


def run(argv, timeout=120):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def local_run(effects, argv, timeout=5):
    """Bound local setup probes while accepting existing argv-only effect runners."""
    try:
        inspect.signature(effects.run).bind(argv, timeout=timeout)
    except (TypeError, ValueError):
        return effects.run(argv)
    return effects.run(argv, timeout=timeout)


def post(url, data, headers):
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
        return response.status


@dataclass
class Effects:
    now: object = time.time
    run: object = run
    post: object = post
    sleep: object = time.sleep
