"""Official tests by default; private calibration fixtures remain explicit."""
from pathlib import Path
import os
import subprocess
import sys

from tools.sync_publishing_checkout import allowed_source

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_configuration_is_reviewed_publishable_source():
    assert allowed_source(Path('pytest.ini'))
    assert (ROOT / 'pytest.ini').read_text().strip() == '[pytest]\ntestpaths = tests'


def test_default_suite_excludes_work_fixtures_but_explicit_audits_remain_available(tmp_path):
    (tmp_path / 'pytest.ini').write_bytes((ROOT / 'pytest.ini').read_bytes())
    tests,work = tmp_path / 'tests',tmp_path / 'work/calibration'
    tests.mkdir(); work.mkdir(parents=True)
    (tests / 'test_official.py').write_text('def test_official():\n    assert True\n')
    private = work / 'test_private.py'
    private.write_text("def test_private():\n    raise AssertionError('This calibration is explicit only')\n")
    keep = {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA',
            'APPDATA','HOMEDRIVE','HOMEPATH','COMSPEC','PATHEXT'}
    env = {key:value for key,value in os.environ.items() if key.upper() in keep}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    options = dict(cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    actual = subprocess.run([sys.executable,'-m','pytest','-q'],**options)
    assert actual.returncode == 0, actual.stdout+actual.stderr
    assert '1 passed' in actual.stdout and 'test_private' not in actual.stdout
    explicit = subprocess.run([sys.executable,'-m','pytest','--collect-only','-q',str(private)],**options)
    assert explicit.returncode == 0 and 'test_private.py::test_private' in explicit.stdout
    assert '1 test collected' in explicit.stdout
