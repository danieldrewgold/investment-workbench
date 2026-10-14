"""
Free mode: block every paid model call in this process.

The workbench calls the Anthropic API from many places with raw httpx. Some of
them sit inside steps that are otherwise free (the IR page finder falls back to
Claude, the macro digest is an Opus call). Unattended jobs such as the filing
watcher enable this so they can never spend money: any request to a paid host
raises PaidCallBlocked, which callers already treat like any other API failure.

    from core import free_mode
    free_mode.enable()

Child processes inherit it through WORKBENCH_FREE_ONLY=1 when they start with
`python -c "import core.free_mode as f; f.enable(); ..."` (see run_free).
"""

from __future__ import annotations

import os
import subprocess
import sys

import httpx

PAID_HOSTS = {"api.anthropic.com"}
ENV_FLAG = "WORKBENCH_FREE_ONLY"


class PaidCallBlocked(RuntimeError):
    pass


_enabled = False


def _check(request: httpx.Request) -> None:
    if request.url.host in PAID_HOSTS:
        raise PaidCallBlocked(f"free mode: blocked paid call to {request.url.host}")


def enable() -> None:
    """Patch httpx (sync + async) so paid hosts are unreachable. Idempotent."""
    global _enabled
    if _enabled:
        return
    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, *args, **kwargs):
        _check(request)
        return sync_send(self, request, *args, **kwargs)

    async def asend(self, request, *args, **kwargs):
        _check(request)
        return await async_send(self, request, *args, **kwargs)

    httpx.Client.send = send
    httpx.AsyncClient.send = asend
    os.environ[ENV_FLAG] = "1"
    _enabled = True


def is_enabled() -> bool:
    return _enabled or os.environ.get(ENV_FLAG) == "1"


def run_free(script: str, *args: str, **popen_kwargs) -> subprocess.Popen:
    """Start `python <script> <args>` in a child process with free mode on."""
    code = ("import sys, runpy, core.free_mode as f; f.enable(); "
            f"sys.argv = {[script, *args]!r}; runpy.run_path({script!r}, run_name='__main__')")
    env = {**os.environ, ENV_FLAG: "1"}
    return subprocess.Popen([sys.executable, "-c", code], env=env, **popen_kwargs)
