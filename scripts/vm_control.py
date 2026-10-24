#!/usr/bin/python3
"""Start or gracefully stop only the dedicated WeType ASR virtual machine."""
import os
import subprocess
import sys
import time

os.environ['LC_ALL'] = 'C'
vm = os.environ.get('WETYPE_VM_NAME', 'wetype-asr')
command = ['/usr/bin/virsh', '--connect', 'qemu:///system']


def state():
    return subprocess.check_output(command + ['domstate', vm], text=True).strip()


if sys.argv[1] == 'start':
    if state() != 'running':
        subprocess.run(command + ['start', vm], check=True)
elif sys.argv[1] == 'stop':
    if state() == 'running':
        helper = os.environ.get('WETYPE_SHUTDOWN_HELPER')
        if helper:
            if not os.path.isabs(helper):
                raise SystemExit('WETYPE_SHUTDOWN_HELPER must be an absolute root-owned executable path')
            subprocess.run([helper], check=True)
        else:
            subprocess.run(command + ['shutdown', vm], check=True)
        deadline = time.monotonic() + 120
        while state() == 'running' and time.monotonic() < deadline:
            time.sleep(2)
        if state() == 'running':
            raise SystemExit('Guest did not stop within 120 seconds; no forced power-off performed.')
else:
    raise SystemExit('Expected start or stop')
