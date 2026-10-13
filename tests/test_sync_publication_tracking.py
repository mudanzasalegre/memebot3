"""Source byte equality is not proof that a clean Git clone contains a file."""
from pathlib import Path
import subprocess

import pytest

from tools.sync_publishing_checkout import apply_plan, build_plan


def git(root: Path, *args: str):
    return subprocess.run(['git','-c','user.name=Source tracking fixture',
        '-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false',
        '-c',f'core.hooksPath={root/"no-hooks"}', *args],
        cwd=root, text=True, capture_output=True, check=True)


def roots(tmp_path, ignore='data/\n'):
    source, destination = tmp_path/'source', tmp_path/'publishing'
    source.mkdir()
    destination.mkdir()
    git(destination, 'init', '-q')
    (destination/'.gitignore').write_text(ignore, encoding='utf-8')
    git(destination, 'add', '.gitignore')
    git(destination, 'commit', '-q', '-m', 'Fixture ignore baseline')
    relative = Path('ui/src/data/routes.ts')
    for root in (source, destination):
        path = root/relative
        path.parent.mkdir(parents=True)
        path.write_text('export const routes = [];\n', encoding='utf-8')
    return source, destination, relative


def test_matching_ignored_source_is_reported_as_missing_from_git(tmp_path):
    source, destination, relative = roots(tmp_path)
    plan = build_plan(source, destination)
    assert len(plan) == 1
    assert plan[0]['path'] == relative.as_posix()
    assert plan[0]['action'] == 'track'
    assert plan[0]['git_tracking_required'] is True and plan[0]['git_ignored'] is True
    before = (destination/relative).stat().st_mtime_ns
    apply_plan(source, destination, plan)
    assert (destination/relative).stat().st_mtime_ns == before
    assert git(destination, 'ls-files', relative.as_posix()).stdout == ''


def test_matching_tracked_source_needs_no_copy_or_tracking(tmp_path):
    source, destination, relative = roots(tmp_path, ignore='')
    git(destination, 'add', relative.as_posix())
    git(destination, 'commit', '-q', '-m', 'Fixture tracked source')
    assert build_plan(source, destination) == []


def test_untracked_nonignored_changes_still_block_source_sync(tmp_path):
    source, destination, _ = roots(tmp_path, ignore='')
    with pytest.raises(ValueError, match='publishing_checkout_has_changes_preserve_them'):
        build_plan(source, destination)


def test_tracking_notice_never_overwrites_a_changed_ignored_target(tmp_path):
    source, destination, relative = roots(tmp_path)
    plan = build_plan(source, destination)
    (destination/relative).write_text('changed by owner\n', encoding='utf-8')
    with pytest.raises(ValueError, match='untracked_target_changed_after_plan'):
        apply_plan(source, destination, plan)
    assert (destination/relative).read_text(encoding='utf-8') == 'changed by owner\n'


def test_route_exception_does_not_publish_operational_or_other_ui_data(tmp_path):
    source, destination, relative = roots(tmp_path)
    template = Path(__file__).resolve().parents[1]/'.gitignore'
    (destination/'.gitignore').write_bytes(template.read_bytes())
    for name, ignored in ((relative.as_posix(), False), ('ui/src/data/raw.json', True),
        ('data/metrics/runtime_events.jsonl', True), ('.env', True), ('ml/model.pkl', True)):
        result = subprocess.run(['git','check-ignore','--quiet','--no-index','--',name],cwd=destination)
        assert result.returncode == (0 if ignored else 1), name
