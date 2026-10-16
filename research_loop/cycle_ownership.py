"""Root-scoped AutoResearch ownership; time/diagnostic JSON never grant it."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import threading

from ml import training_lock as locks
from research_loop.paths import project_root,research_runs_dir


class AutoResearchAlreadyRunning(RuntimeError):
    def __init__(self):
        super().__init__('autoresearch_already_running')


@dataclass
class _Session:
    cycle_active: bool = False


_local=threading.local()


def _scope(root):
    resolved=project_root(root).resolve()
    # Thread-local context cannot be borrowed by asyncio.to_thread; reset an
    # inherited fork context too. The OS handle is the actual process owner.
    if getattr(_local,'pid',None)!=os.getpid():
        _local.pid=os.getpid();_local.sessions={}
    return resolved,os.path.normcase(str(resolved)),_local.sessions


@contextmanager
def autoresearch_session(root: str|Path|None=None):
    """One CLI/service or standalone mutation owner before any state writes.

    Reuse the tested OS-held lock mechanism at a distinct research-only path;
    this neither acquires training.lock nor authorizes training/activation.
    TTL remains diagnostic and cannot evict an alive process/thread.
    """
    resolved,key,sessions=_scope(root)
    marker=research_runs_dir(resolved)/'autoresearch_cycle.lock'
    if key in sessions or not locks.acquire_lock(marker):
        raise AutoResearchAlreadyRunning()
    owner=_Session();sessions[key]=owner
    try:
        yield owner
    finally:
        sessions.pop(key,None)
        locks.release_lock(marker)


@contextmanager
def cycle_scope(root: str|Path|None=None):
    """Borrow only this actual thread's service owner; cycles never reenter."""
    resolved,key,sessions=_scope(root)
    owner=sessions.get(key)
    if owner is None:
        with autoresearch_session(resolved):
            with cycle_scope(resolved):
                yield
        return
    if owner.cycle_active:
        raise AutoResearchAlreadyRunning()
    owner.cycle_active=True
    try:
        yield
    finally:
        owner.cycle_active=False


@contextmanager
def mutation_scope(root: str|Path|None=None):
    """A descendant demotion belongs to the current cycle/service owner."""
    resolved,key,sessions=_scope(root)
    if key in sessions:
        yield
    else:
        with autoresearch_session(resolved):
            yield
