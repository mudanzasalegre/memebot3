"""Test-only sitecustomize: guard the owned native report subprocess too."""
import atexit
import json
import os
from pathlib import Path
import sys
import threading
import time


def install(root):
    root = Path(root).resolve()
    assert Path.cwd().resolve() == root and (root/'run_bot.py').is_file()
    log = root/'native-worker-guard.jsonl'
    worker = Path(sys.argv[0]).resolve() == root/'tools/regenerate_core_reports.py'
    assert worker or Path(sys.argv[0]).resolve() == root/'tests/native_supervised_case.py'
    def record(event, **fields):
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'event':event, 'pid':os.getpid(), **fields})+'\n')
    def profile(frame, event, arg):
        module = str(frame.f_globals.get('__name__') or '')
        if (event == 'call' and module.startswith(('ml.', 'sklearn.', 'research_loop.'))
                and frame.f_code.co_name in {'fit', 'partial_fit', 'train', 'train_model',
                    'run_training_cycle', 'train_runner_advisory', 'run_autoresearch_cycle'}):
            record('blocked_fit', function=frame.f_code.co_name)
            raise PermissionError('Fitting and AutoResearch forbidden in native runtime fixture')
    sys.setprofile(profile)
    threading.setprofile(profile)
    if not worker:
        return

    operators = [Path('D:/Dev/Python/memebot3').resolve(),
        Path('C:/Users/monmo10/Documents/GitHub/memebot3').resolve()]
    def audit(event, args):
        if event in {'socket.getaddrinfo', 'socket.connect'}:
            address = args[0] if event == 'socket.getaddrinfo' else args[1][0]
            if address not in {'127.0.0.1', '::1', 'localhost', None}:
                record('blocked_network')
                raise PermissionError('Report child cannot access external network')
        elif event == 'subprocess.Popen':
            record('blocked_subprocess')
            raise PermissionError('Report child cannot launch another process')
        elif event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(root):
                return
            for operator in operators:
                if path.is_relative_to(operator) and not path.is_relative_to(operator/'.venv'):
                    record('blocked_operator_file')
                    raise PermissionError('Report child cannot read operator project files')
    sys.addaudithook(audit)
    record('worker_started')
    atexit.register(lambda: record('worker_exit'))
    if os.environ.get('MEMEBOT_HOLD_REPORT_CHILD') == '1':
        deadline = time.monotonic()+100
        while not (root/'release-report-child').exists():
            if time.monotonic() >= deadline:
                record('hold_timeout')
                raise RuntimeError('Owned report child release was not delivered')
            time.sleep(.01)
        record('worker_released')


if os.environ.get('MEMEBOT_SUPERVISED_TEST_ROOT'):
    try:
        install(os.environ['MEMEBOT_SUPERVISED_TEST_ROOT'])
    except BaseException:
        # Python normally continues after a sitecustomize error. This owned
        # test process must instead fail closed BEFORE importing the runtime.
        os._exit(91)
