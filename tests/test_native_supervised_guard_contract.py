"""The old no-child fixture only permits one exact owned report invocation."""
import ast
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def child_contract(tmp_path):
    node = next(node for node in ast.parse(Path('tests/native_paper_lifecycle_case.py').read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == 'owned_report_child')
    ns = {'Path':Path, 'subprocess':subprocess, 'expected':tmp_path,
        'sys':SimpleNamespace(argv=[str(tmp_path/'tests/native_supervised_case.py')], executable=sys.executable),
        'options':{'supervised_runtime':True}, 'allowed_report_children':[]}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'native-child-contract', 'exec'), ns)
    command = [sys.executable, str(tmp_path/'tools/regenerate_core_reports.py')]
    return ns, [sys.executable, command, str(tmp_path), None]


@pytest.mark.parametrize('encoded', [False, True])
def test_one_exact_owned_report_child_is_allowed(tmp_path, encoded):
    ns, args = child_contract(tmp_path)
    if encoded: args[1] = subprocess.list2cmdline(args[1])
    assert ns['owned_report_child'](args) is True


@pytest.mark.parametrize('bad', ['disabled','entry_script','cwd','command','extra_argument',
    'environment','executable','second_child'])
def test_no_other_child_or_environment_can_pass_native_guard(tmp_path, bad):
    ns, args = child_contract(tmp_path)
    if bad == 'disabled': ns['options']['supervised_runtime'] = False
    elif bad == 'entry_script': ns['sys'].argv = [str(tmp_path/'tests/other.py')]
    elif bad == 'cwd': args[2] = str(tmp_path.parent)
    elif bad == 'command': args[1] = [sys.executable, '-m', 'ml.train']
    elif bad == 'extra_argument': args[1].append('--unexpected')
    elif bad == 'environment': args[3] = {'PYTHONPATH':'other'}
    elif bad == 'executable': args[0] = str(tmp_path/'other-python')
    else: ns['allowed_report_children'].append('already-owned')
    assert ns['owned_report_child'](args) is False
