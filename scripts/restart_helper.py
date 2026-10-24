#!/usr/bin/python3
"""Root-owned, fixed allowlist restart helper. Never install writable by the service user."""
import os
import secrets
import subprocess
import sys

TARGETS={'asr':['wetype-asr-vm.service','wetype-iat.service'],
         'search':['wetype-asr-console.service']}


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in TARGETS:
        raise SystemExit('Expected exactly one argument: asr or search')
    if os.geteuid()!=0:
        raise SystemExit('This fixed helper must be invoked through the restricted sudoers rule')
    name=sys.argv[1]
    unit='wetype-console-restart-'+name+'-'+secrets.token_hex(6)
    subprocess.run(['/usr/bin/systemd-run','--quiet','--collect','--unit='+unit,
        '--on-active=2s','--timer-property=RemainAfterElapse=no',
        '/usr/bin/systemctl','restart',*TARGETS[name]],check=True,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
    print(unit)


if __name__=='__main__':
    main()
