import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import hashlib
import hmac
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlencode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import wave

from fastapi.testclient import TestClient
import httpx
import pytest

from gateway import console, iat


@pytest.fixture
def configured(tmp_path,monkeypatch):
    subprocess.run([sys.executable,'scripts/init_config.py','--data-dir',str(tmp_path),'--upstream','http://upstream.invalid:8765'],check=True,capture_output=True)
    config=json.loads((tmp_path/'config.json').read_text())
    monkeypatch.setattr(iat,'CONFIG',tmp_path/'config.json')
    monkeypatch.setattr(console,'ASR_CONFIG',tmp_path/'config.json')
    monkeypatch.setattr(console,'BASE_DIR',tmp_path)
    monkeypatch.setattr(console,'KEY_STORE',tmp_path/'api_keys.json')
    monkeypatch.setattr(console,'CONSOLE_TOKEN_FILE',tmp_path/'console_token')
    console.HEALTH_CACHE.clear()
    console.RESTART_LAST.clear()
    return config,tmp_path


def sign(config,*,bad=False,date=None):
    date=date or formatdate(usegmt=True)
    origin=f'host: testserver\ndate: {date}\nGET /v1 HTTP/1.1'
    signature=base64.b64encode(hmac.new(config['iat']['api_secret'].encode(),origin.encode(),hashlib.sha256).digest()).decode()
    auth=f'api_key="{config["iat"]["api_key"]}", algorithm="hmac-sha256", headers="host date request-line", signature="{"invalid" if bad else signature}"'
    return '/v1?'+urlencode({'host':'testserver','date':date,'authorization':base64.b64encode(auth.encode()).decode()})


def first_frame(config,**overrides):
    frame={'header':{'app_id':config['iat']['app_id'],'status':0},
        'parameter':{'iat':{'domain':'spark_asr_v2'}},
        'payload':{'audio':{'encoding':'raw','sample_rate':16000,'channels':1,'bit_depth':16,'seq':1,'status':0,'audio':base64.b64encode(b'\0\0'*640).decode()}}}
    frame['payload']['audio'].update(overrides)
    return frame


@pytest.mark.parametrize('case',['bad-signature','expired','host-mismatch'])
def test_hmac_handshake_rejects_invalid_auth(configured,case):
    config,_=configured
    url=sign(config,bad=case=='bad-signature',date='Mon, 01 Jan 2001 00:00:00 GMT' if case=='expired' else None)
    if case=='host-mismatch':
        url=url.replace('host=testserver','host=other.invalid')
    from starlette.websockets import WebSocketDisconnect
    with TestClient(iat.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url):
                pass


def test_websocket_buffers_pcm_and_returns_final_result(configured,monkeypatch):
    config,_=configured
    requests=[]
    original=httpx.AsyncClient
    def transport(request):
        requests.append(request)
        assert request.headers['authorization']=='Bearer '+config['api_key']
        assert b'RIFF' in request.content and b'wetype-voice' in request.content
        return httpx.Response(200,json={'text':'协议测试成功。'})
    monkeypatch.setattr(iat.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(transport),**kwargs))
    with TestClient(iat.app) as client:
        with client.websocket_connect(sign(config)) as ws:
            ws.send_json(first_frame(config))
            assert ws.receive_json()['header']['status']==0
            assert requests==[]
            ws.send_json({'header':{'status':2},'payload':{'audio':{'seq':2,'status':2,'audio':''}}})
            final=ws.receive_json()
            assert final['header']['code']==0 and final['header']['status']==2
            result=json.loads(base64.b64decode(final['payload']['result']['text']))
            assert result['ws'][0]['cw'][0]['w']=='协议测试成功。'
    assert len(requests)==1


@pytest.mark.parametrize('case',['appid','encoding','sequence','duration','format'])
def test_invalid_frames_rejected(configured,case):
    config,_=configured
    frame=first_frame(config)
    if case=='appid': frame['header']['app_id']='invalid'
    if case=='encoding': frame['payload']['audio']['encoding']='lame'
    if case=='sequence': frame['payload']['audio']['seq']=None
    if case=='duration': frame['payload']['audio']['audio']=base64.b64encode(b'\0'*(16000*2*60+2)).decode()
    if case=='format': frame['payload']['audio']['sample_rate']=44100
    with TestClient(iat.app) as client:
        with client.websocket_connect(sign(config)) as ws:
            ws.send_json(frame)
            result=ws.receive_json()
            if result['header']['code']==0:
                result=ws.receive_json()
            assert result['header']['code']!=0


def test_existing_configuration_cannot_be_overwritten(configured):
    _,path=configured
    before=(path/'config.json').read_bytes()
    result=subprocess.run([sys.executable,'scripts/init_config.py','--data-dir',str(path)],capture_output=True)
    assert result.returncode!=0
    assert (path/'config.json').read_bytes()==before


def test_api_scope_and_concurrent_usage_updates(configured):
    config,_=configured
    assert console.find_key(config['api_key'],scope='search') is None
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda _:console.find_key(config['api_key'],scope='asr',touch=True),range(40)))
    assert all(results)
    assert console.list_keys()[0]['usage']['asr']==40


def test_console_control_authentication_and_allowlist(configured,monkeypatch):
    config,path=configured
    server=console.ThreadingHTTPServer(('127.0.0.1',0),console.Handler)
    origin=f'http://127.0.0.1:{server.server_port}'
    config['allowed_console_origins']=[origin]
    config['enable_restart']=True
    (path/'config.json').write_text(json.dumps(config))
    token=(path/'console_token').read_text().strip()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=origin,trust_env=False) as client:
            assert client.get('/console-api/asr-config').status_code==401
            headers={'X-Console-Token':token,'Origin':origin}
            monkeypatch.setattr(console,'backend_status',lambda:{'csrf_token':console.CONTROL_CSRF_TOKEN})
            assert client.get('/console-api/status',headers=headers).status_code==200
            assert client.post('/console-api/control/restart',headers=headers,json={'service':'asr'}).status_code==403
            headers['X-Console-Action']=console.CONTROL_CSRF_TOKEN
            assert client.post('/console-api/control/restart',headers={**headers,'Origin':'https://evil.invalid'},json={'service':'asr'}).status_code==403
            assert client.post('/console-api/control/restart',headers=headers,json={'service':'sshd'}).status_code==400
            assert client.post('/console-api/keys',headers={**headers,'X-Console-Action':''},json={'name':'test'}).status_code==403
            assert client.post('/console-api/keys',headers=headers,json={'name':'test','scopes':['search']}).status_code==201
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


@pytest.mark.parametrize('mode',['multipart','base64'])
def test_legacy_http_transcription_uses_configured_bridge(configured,mode):
    config,path=configured
    upstream_requests=[]
    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            upstream_requests.append((self.path,self.headers.get('Authorization'),body))
            payload=json.dumps({'text':'文件上传测试成功。'},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def log_message(self,*args): pass
    bridge=ThreadingHTTPServer(('127.0.0.1',0),Bridge)
    config['upstream']=f'http://127.0.0.1:{bridge.server_port}'
    (path/'config.json').write_text(json.dumps(config))
    server=console.ThreadingHTTPServer(('127.0.0.1',0),console.Handler)
    threads=[threading.Thread(target=item.serve_forever,daemon=True) for item in (bridge,server)]
    for thread in threads: thread.start()
    recording=io.BytesIO()
    with wave.open(recording,'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\0\0'*640)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            audio=recording.getvalue()
            args={'files':{'file':('recording.wav',audio,'audio/wav')}} if mode=='multipart' else {'json':{'audio_base64':base64.b64encode(audio).decode(),'filename':'recording.wav'}}
            response=client.post('/api/asr',headers={'Authorization':'Bearer '+config['api_key']},**args)
            assert response.status_code==200
            assert response.json()['text']=='文件上传测试成功。'
            assert response.json()['model']=='wetype-voice'
            assert client.post('/api/asr').status_code==401
        assert upstream_requests[0][0]=='/v1/audio/transcriptions'
        assert upstream_requests[0][1]=='Bearer '+config['api_key']
        assert b'RIFF' in upstream_requests[0][2]
    finally:
        for item in (server,bridge): item.shutdown();item.server_close()
        for thread in threads: thread.join(timeout=3)
