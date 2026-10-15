"""Provider-free subprocess fixture, not the operational bot or its config."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--source-root', type=Path, required=True)
parser.add_argument('--ignore-stop', action='store_true')
args = parser.parse_args()
root = args.root.resolve()
source_root = args.source_root.resolve()
assert root.is_dir() and root != source_root
sys.path.insert(0, str(source_root))
from runtime import local_stop as stop
from runtime.loop_scheduler import supervise
from runtime.owned_thread import await_owned_thread
from runtime.single_instance import SingleInstanceLock

binding = stop.StopBinding.create(root)
lock = SingleInstanceLock(root / 'data' / 'run_bot.lock')
lock.acquire(payload=binding.owner_fields())
(root / 'owner.json').write_text(json.dumps(binding.owner_fields()))


def owned_io():
    (root / 'io-started').write_text('ready')
    # Normal QA independently bounds cold controller startup90s, delivery30s
    # and final cleanup. This self-expiry is not a production stop deadline.
    deadline = time.monotonic() + (45 if args.ignore_stop else 180)
    while not (root / 'release').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Synthetic IO release was not received')
        time.sleep(.005)
    (root / 'io-finished').write_text('complete')


async def runtime():
    try:
        await await_owned_thread(owned_io)
        # A failed parent fixture cannot leave an unbounded orphan waiting.
        await asyncio.sleep(5)
        raise RuntimeError('Synthetic owner received no matching stop')
    finally:
        (root / 'cleanup-finished').write_text('complete')


try:
    asyncio.run(runtime() if args.ignore_stop else stop.run_with_local_stop(runtime, binding))
except stop.LocalStopRequested:
    assert (root / 'io-finished').is_file() and (root / 'cleanup-finished').is_file()
    binding.complete()
    (root / 'stopped').write_text('after-owned-cleanup')
finally:
    binding.clear_request()
    lock.release()
