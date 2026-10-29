"""The clock, subprocesses and HTTP are injected at the application boundary."""
import os
import signal
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


def call(argv, text, timeout):
    """Run argv with text on stdin. On timeout, kill its whole process group so a CLI's children
    cannot keep the pipes open, then re-raise TimeoutExpired."""
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8', errors='replace', start_new_session=True) as proc:
        try:
            out, err = proc.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def post(url, data, headers):
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
        return response.status


@dataclass
class Effects:
    now: object = time.time
    run: object = run
    call: object = call
    post: object = post
    sleep: object = time.sleep
