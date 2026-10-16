"""Root-scoped AutoResearch ownership; time/diagnostic JSON never grant it."""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import signature
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


def owned_mutation(function):
    """Freeze a synchronous management API's root before its first read/write.

    Bind using the original signature so positional roots and argument errors
    keep their normal contract. Nested management calls borrow the actual
    thread's owner; a competing process/thread fails before entering the body.
    APIs with explicit output paths must also bind them with owned_artifact.
    """
    parameters=signature(function)
    if 'root' not in parameters.parameters:
        raise TypeError('owned_research_mutation_requires_root')
    @wraps(function)
    def owned(*args,**kwargs):
        bound=parameters.bind(*args,**kwargs)
        root=project_root(bound.arguments.get('root')).resolve()
        bound.arguments['root']=root
        with mutation_scope(root):
            return function(*bound.args,**bound.kwargs)
    return owned


def owned_artifact(path: str|Path,root: str|Path) -> Path:
    """Resolve project-relative artifacts inside the actual owned project.

    Canonical resolution also rejects an existing symlink into another root.
    This is cooperative management ownership, not a hostile filesystem sandbox.
    """
    root=project_root(root).resolve()
    path=Path(path)
    path=(path if path.is_absolute() else root/path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('research_artifact_outside_owned_root')
    return path
