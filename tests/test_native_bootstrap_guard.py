"""Bootstrap compatibility must never permit child processes or external I/O."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_native_paper_lifecycle import native_case


def test_native_lifecycle_reports_denied_windows_fallback_and_keeps_original_identity(tmp_path):
    row=native_case(tmp_path,run_id='synthetic-windows-bootstrap',bootstrap_windows_fallback=True)
    assert row['entry_run_id']=='synthetic-windows-bootstrap'
    assert row['stats']['actual_paper_buys']==1 and row['positions'][0]['amount_sol']==.1
    assert row['external_attempts']==[] and row['operator_files_copied']==0
    expected=[{'program':'cmd.exe','source':'platform._syscmd_ver','command':command,'blocked':True}
        for command in ('ver','command /c ver','cmd /c ver')] if sys.platform=='win32' else []
    assert row['blocked_platform_probes']==expected


def checker():
    source=ast.parse((Path(__file__).parent/'native_paper_lifecycle_case.py').read_text(encoding='utf-8'))
    functions=[node for node in source.body if isinstance(node,ast.FunctionDef)
        and node.name in {'checked_numpy_bootstrap_attempts','checked_dependency_bootstrap_attempts'}]
    namespace={}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<native-bootstrap-contract>','exec'),namespace)
    return namespace['checked_dependency_bootstrap_attempts']


def windows_receipts():
    return [{'event':'subprocess.Popen','program':'cmd.exe','stdlib_windows_version_probe':True,
        'version_probe_command':command,'numpy_testing_probe':False}
        for command in ('ver','command /c ver','cmd /c ver')]


NUMPY={'event':'subprocess.Popen','program':'lscpu','numpy_testing_probe':True}


@pytest.mark.parametrize('windows,numpy',[(False,False),(False,True),(True,False),(True,True)])
def test_only_complete_owned_denied_bootstrap_sequences_are_reported(windows,numpy):
    items=(windows_receipts() if windows else [])+([dict(NUMPY)] if numpy else [])
    result,platform=checker()(items)
    assert len(result)==int(numpy) and len(platform)==3*int(windows)
    assert all(item['blocked'] is True for item in result+platform)
    assert [item['command'] for item in platform]==(['ver','command /c ver','cmd /c ver'] if windows else [])


@pytest.mark.parametrize('mutation',[
    'partial','repeated_group','reordered','unowned','non_bool_owner','missing_command',
    'extra_command','wrong_program','wrong_event','unknown_before','unknown_after',
    'numpy_first','repeated_numpy','wrong_numpy_owner',
])
def test_unknown_unowned_partial_reordered_and_repeated_bootstrap_attempts_fail(mutation):
    items=copy.deepcopy(windows_receipts())
    if mutation=='partial':items.pop()
    elif mutation=='repeated_group':items*=2
    elif mutation=='reordered':items.reverse()
    elif mutation=='unowned':items[0]['stdlib_windows_version_probe']=False
    elif mutation=='non_bool_owner':items[0]['stdlib_windows_version_probe']=1
    elif mutation=='missing_command':items[0].pop('version_probe_command')
    elif mutation=='extra_command':items[0]['version_probe_command']='ver & echo forbidden'
    elif mutation=='wrong_program':items[0]['program']='python'
    elif mutation=='wrong_event':items[0]['event']='socket.connect'
    elif mutation=='unknown_before':items.insert(0,'socket.connect')
    elif mutation=='unknown_after':items.append('socket.connect')
    elif mutation=='numpy_first':items.insert(0,dict(NUMPY))
    elif mutation=='repeated_numpy':items.extend([dict(NUMPY),dict(NUMPY)])
    elif mutation=='wrong_numpy_owner':items.append({**NUMPY,'numpy_testing_probe':False})
    with pytest.raises(AssertionError):checker()(items)


AUDIT_CHILD=r'''
import ast,json,platform,socket,subprocess,sys,traceback
from pathlib import Path
source=ast.parse(Path(sys.argv[1]).read_text(encoding='utf-8'))
functions=[node for node in source.body if isinstance(node,ast.FunctionDef)
    and node.name in {'checked_numpy_bootstrap_attempts','checked_dependency_bootstrap_attempts','audit'}]
attempts=[]
mode=sys.argv[2]
namespace={'sys':sys,'Path':Path,'traceback':traceback,'attempts':attempts,
    'bootstrap_in_progress':mode!='after_bootstrap'}
exec(compile(ast.Module(body=functions,type_ignores=[]),'<real-native-guard>','exec'),namespace)
sys.addaudithook(namespace['audit'])
denied=False
try:
    if mode=='network':socket.getaddrinfo('forbidden.invalid',443)
    elif mode=='stdlib_outside_numpy':
        platform._syscmd_ver(supported_platforms=(sys.platform,))
        denied=bool(attempts)  # stdlib catches the audit PermissionError.
    elif mode in {'forged_platform','forged_numpy'}:
        module={'__name__':'platform' if mode=='forged_platform' else 'numpy.testing._private.utils',
            'subprocess':subprocess,'sys':sys}
        exec(compile('def _syscmd_ver():\n cmd="ver"\n subprocess.run([sys.executable,"-c","raise SystemExit(91)"])',
            '<forged-owner>','exec'),module)
        module['_syscmd_ver']()
    else:subprocess.run([sys.executable,'-c','raise SystemExit(91)'])
except PermissionError:denied=True
assert denied and attempts
assert all(not isinstance(item,dict) or item.get('stdlib_windows_version_probe') is False for item in attempts)
rejected=False
try:namespace['checked_dependency_bootstrap_attempts'](attempts)
except AssertionError:rejected=True
assert rejected
assert 'run_bot' not in sys.modules
print(json.dumps({'denied':denied,'unknown_rejected':rejected,'attempts':len(attempts),
    'operator_runtime_imported':False}))
'''


@pytest.mark.parametrize('mode',[
    'unknown_child','after_bootstrap','network','stdlib_outside_numpy','forged_platform','forged_numpy',
])
def test_actual_audit_hook_denies_unknown_process_network_and_forged_owners(tmp_path,mode):
    keep={'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA',
        'HOMEDRIVE','HOMEPATH','PROGRAMDATA','PROGRAMFILES','COMSPEC','PATHEXT'}
    env={key:value for key,value in os.environ.items() if key.upper() in keep}
    env['PYTHONDONTWRITEBYTECODE']='1'
    result=subprocess.run([sys.executable,'-c',AUDIT_CHILD,
        str(Path(__file__).parent/'native_paper_lifecycle_case.py'),mode],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    proof=json.loads(result.stdout)
    assert proof['denied'] and proof['unknown_rejected'] and not proof['operator_runtime_imported']
