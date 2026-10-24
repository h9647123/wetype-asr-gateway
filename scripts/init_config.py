"""Generate private local configuration without printing credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default='./data')
    parser.add_argument('--upstream',default='http://127.0.0.1:8765')
    parser.add_argument('--upstream-key-file',type=Path)
    parser.add_argument('--public-base',help='HTTPS public origin, e.g. https://asr.example.com')
    parser.add_argument('--console-origin',action='append',default=[])
    parser.add_argument('--enable-restart',action='store_true',help='Requires separately installed root-owned helper and sudoers')
    args=parser.parse_args()
    data=Path(args.data_dir)
    data.mkdir(parents=True,exist_ok=True)
    os.chmod(data,0o700)
    files=('config.json','console_token','api_keys.json','bridge-api-key.txt')
    if any((data/name).exists() for name in files):
        parser.error('Existing configuration detected; refusing to rotate or overwrite credentials')
    upstream=args.upstream.rstrip('/')
    parsed=urlsplit(upstream)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
        parser.error('Upstream must be an HTTP(S) origin without embedded credentials')
    public=(args.public_base or '').rstrip('/')
    if public and (urlsplit(public).scheme!='https' or urlsplit(public).path or urlsplit(public).username):
        parser.error('--public-base must be an HTTPS origin without a path or embedded credentials')
    key=args.upstream_key_file.read_text().strip() if args.upstream_key_file else 'sk-'+secrets.token_urlsafe(32)
    if len(key)<24 or any(ch.isspace() for ch in key):
        parser.error('Use a strong upstream key with at least 24 characters and no whitespace')
    origins=args.console_origin or ['http://127.0.0.1:18080']
    for origin in origins:
        item=urlsplit(origin)
        if item.scheme not in ('http','https') or not item.netloc or item.path or item.username:
            parser.error('Console origins must be complete HTTP(S) origins without a path')
    config={'upstream':upstream,'api_key':key,'model':'wetype-voice',
        'legacy_url':public+'/api/asr' if public else 'http://127.0.0.1:18080/api/asr',
        'openai_base_url':public+'/v1' if public else upstream+'/v1',
        'iat_url':public.replace('https://','wss://',1)+'/v1' if public else 'ws://127.0.0.1:8768/v1',
        'iat_health_url':'http://127.0.0.1:8768/health','enable_restart':args.enable_restart,
        'allowed_console_origins':origins,
        'iat':{'app_id':secrets.token_hex(4),'api_key':secrets.token_hex(16),'api_secret':secrets.token_hex(16)}}
    record={'id':secrets.token_hex(8),'name':'initial-asr-key','sha256':hashlib.sha256(key.encode()).hexdigest(),
        'masked':key[:7]+'*'*24+key[-4:],'scopes':['asr'],'enabled':True,
        'created_at':datetime.now(timezone.utc).isoformat(),'last_used_at':None,'source':'initialization'}
    contents={'config.json':json.dumps(config,ensure_ascii=False,indent=2)+'\n',
        'console_token':secrets.token_urlsafe(40)+'\n','api_keys.json':json.dumps({'keys':[record]},indent=2)+'\n',
        'bridge-api-key.txt':key+'\n'}
    for name,content in contents.items():
        fd=os.open(data/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(content)
    print('Private configuration created in',data.resolve())
    print('Copy bridge-api-key.txt privately to the Windows bridge. No credentials were printed.')
    print('Use scripts/console_url.py to display the private console login URL locally.')


if __name__=='__main__':
    main()
