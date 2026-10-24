#!/opt/wetype-asr-gateway/.venv/bin/python
"""Optional root-owned guest shutdown helper when the VM ignores ACPI.

Private JSON: {"ip":"...", "username":"...", "password":"..."}.
Set WETYPE_VM_CREDENTIALS for this helper's systemd environment.
"""
import json
import os
from pathlib import Path

import winrm

config=json.loads(Path(os.environ.get('WETYPE_VM_CREDENTIALS','/etc/wetype-asr/vm-credentials.json')).read_text())
session=winrm.Session('https://'+config['ip']+':5986/wsman',auth=(config['username'],config['password']),
    transport='ntlm',server_cert_validation=config.get('certificate_validation','validate'),
    operation_timeout_sec=30,read_timeout_sec=40)
result=session.run_cmd('shutdown.exe',['/s','/t','5','/d','p:4:1'])
if result.status_code:
    raise SystemExit('Guest shutdown request failed; inspect private WinRM configuration')
print('Guest accepted an orderly Windows shutdown request.')
