"""Exclusive management identities, not a hostile-filesystem sandbox."""
from pathlib import Path
from research_loop.cycle_ownership import owned_artifact


class ResearchIdentityAlreadyUsed(RuntimeError):
    pass


def claim_directory(path: Path, *, root: Path, reason: str) -> Path:
    path=owned_artifact(path,root)
    try:
        path.mkdir(parents=True,exist_ok=False)
    except FileExistsError:
        raise ResearchIdentityAlreadyUsed(reason) from None
    return path


def claim_snapshot(directory: Path, name: str, *, root: Path, artifacts: tuple[str,...], reason: str) -> Path:
    directory=owned_artifact(directory,root)
    if any((directory/leaf).exists() or (directory/leaf).is_symlink() for leaf in artifacts):
        raise ResearchIdentityAlreadyUsed(reason)
    return claim_directory(directory/name,root=root,reason=reason)
