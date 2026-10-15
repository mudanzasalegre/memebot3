"""Request a bounded cooperative stop, without importing bot/config/providers.

The stack controller verifies repository command line/creation identity first.
This helper only addresses the fresh native owner's nonce; it never kills.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.local_stop import StopProtocolError, read_owner_binding, request_and_wait
from runtime.process_observation import ProcessObservation, ProcessObservationError


def request_runtime_stop(root, *, expected_pid, timeout=15., observation_factory=ProcessObservation):
    binding = read_owner_binding(root, expected_pid=expected_pid)
    with observation_factory(binding.pid) as observed:
        # Refuse a replacement owner between reading the lock and holding its
        # process object. A stale PID/nonce is not a new generation's command.
        if read_owner_binding(root, expected_pid=expected_pid) != binding:
            raise StopProtocolError('Native stop owner changed')
        return request_and_wait(binding, is_running=observed.is_running, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-pid', required=True, type=int)
    parser.add_argument('--timeout', type=float, default=15.)
    args = parser.parse_args(argv)
    try:
        result = request_runtime_stop(ROOT, expected_pid=args.expected_pid, timeout=args.timeout)
    except (StopProtocolError, ProcessObservationError, OSError):
        result = {'pid': args.expected_pid, 'status': 'cooperative_unavailable',
                  'requested': False, 'owner_exited': False, 'cleanup_confirmed': False}
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result['owner_exited'] and result['cleanup_confirmed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
