"""Local Xfyun v2 wire-format adapter; streams supported PCM and returns replacement snapshots.

No Xfyun cloud connection. Actual recognition is WeType via the Windows VM.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import os
import json
import re
import secrets
import time
import wave
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

def stream_url(config):
    # Legacy HTTP may point at a separate wrapper; override only the native WS
    # target without altering console, file-ASR routes or existing credentials.
    base = urlsplit(config.get('stream_upstream') or config['upstream'])
    if base.scheme not in ('http', 'https') or not base.netloc or base.username or base.password:
        raise ValueError('invalid_upstream_configuration')
    return urlunsplit(('wss' if base.scheme == 'https' else 'ws', base.netloc,
                      base.path.rstrip('/')+'/v1/audio/stream', '', ''))
MAX_AUDIO_SECONDS = 60
UPLOAD_TIMEOUT = 90
FRAME_TIMEOUT = 15
FINAL_TIMEOUT = 60

CONFIG = Path(os.environ.get('WETYPE_CONFIG', str(Path(os.environ.get('WETYPE_DATA_DIR', './data')) / 'config.json')))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
backend_lock = asyncio.Semaphore(1)
active_sessions = 0


# Public package deliberately keeps no on-disk audio archive. The production
# deployment may add private recording hooks; it is not bundled here.
async def archive(data, filename, source, metadata):
    return (None, {})


async def update(record, **values):
    return None


def wav_bytes(pcm, params):
    output = io.BytesIO()
    with wave.open(output, 'wb') as recording:
        recording.setframerate(params[0])
        recording.setnchannels(params[1])
        recording.setsampwidth(params[2] // 8)
        recording.writeframes(pcm)
    return output.getvalue()


async def forward(record, config, data):
    # Preserve the old non-streaming HTTP contract for other PCM formats.
    async with asyncio.timeout(240):
        async with backend_lock:
            async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                return await client.post(config['upstream'].rstrip('/') + '/v1/audio/transcriptions',
                    headers={'Authorization': 'Bearer ' + config['api_key']},
                    data={'model': 'wetype-voice'}, files={'file': ('audio.wav', data, 'audio/wav')})


def validate_auth(ws, config):
    q = ws.query_params
    if q.get('host') != ws.headers.get('host'):
        raise ValueError('host_mismatch')
    date = q.get('date', '')
    dt = parsedate_to_datetime(date)
    if dt.tzinfo is None or abs(time.time() - dt.timestamp()) > 300:
        raise ValueError('expired_signature')
    auth = base64.b64decode(q.get('authorization', ''), validate=True).decode('ascii')
    match = re.fullmatch(r'api_key="([^"]+)",\s*algorithm="hmac-sha256",\s*headers="host date request-line",\s*signature="([^"]+)"', auth)
    if not match or not hmac.compare_digest(match[1], config['iat']['api_key']):
        raise ValueError('invalid_api_key')
    canonical = f"host: {q['host']}\ndate: {date}\nGET {ws.url.path} HTTP/1.1"
    expected = base64.b64encode(hmac.new(config['iat']['api_secret'].encode(), canonical.encode(), hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, match[2]):
        raise ValueError('invalid_signature')


@app.get('/health')
def health():
    return {'ok': True, 'protocol': 'xfyun-v2-compatible', 'streaming_results': True, 'streaming_implemented': True,
            'native_health_verified': False, 'streaming_pcm': {'sample_rate': 16000, 'channels': 1, 'bit_depth': 16},
            'other_pcm_mode': 'buffered-http', 'max_audio_seconds': MAX_AUDIO_SECONDS, 'audio_encoding': ['raw'], 'recording_archive': False}


async def send_snapshot(ws, sid, text, revision, final):
    """A native response is a complete, revisable hypothesis, never an append."""
    status = 2 if final else 1
    result = {'ret': 0, 'sn': 0, 'ls': final, 'pgs': 'rpl', 'rg': [0, 0],
              'ws': [{'bg': 0, 'cw': [{'w': text}]}]}
    encoded = base64.b64encode(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()).decode()
    await ws.send_json({'header': {'code': 0, 'message': 'success', 'sid': sid, 'status': status},
        'payload': {'result': {'compress': 'raw', 'encoding': 'utf8', 'format': 'json',
                              'seq': revision, 'status': status, 'text': encoded}}})


async def stream_native(config, chunks, ws, sid, outcome):
    """Own one upstream socket; upload and receive cannot block each other."""
    started = time.monotonic()
    upstream = None
    complete = False
    try:
        # Shared with the buffered fallback below: one gateway-side native slot.
        async with asyncio.timeout(UPLOAD_TIMEOUT + FINAL_TIMEOUT):
            async with backend_lock:
                outcome['queue_ms'] = round((time.monotonic()-started)*1000, 1)
                async with connect(stream_url(config), additional_headers={'Authorization': 'Bearer '+config['api_key']},
                                   open_timeout=15, close_timeout=3, max_size=1024*1024,
                                   proxy=None) as upstream:
                    ready = json.loads(await asyncio.wait_for(upstream.recv(), 30))
                    if ready.get('type') != 'ready' or ready.get('sample_rate') != 16000 or ready.get('channels') != 1 or ready.get('format') != 's16le':
                        raise ValueError('invalid_upstream_ready')
                    ended = asyncio.Event()
                    async def send_audio():
                        while True:
                            chunk = await chunks.get()
                            if chunk is None:
                                await upstream.send(json.dumps({'type': 'end'}))
                                ended.set()
                                return
                            await upstream.send(chunk)
                    async def receive_results():
                        last_revision = -1
                        while True:
                            # Native may legitimately take time to return its first text.
                            raw = await asyncio.wait_for(upstream.recv(), FINAL_TIMEOUT)
                            item = json.loads(raw)
                            if item.get('type') == 'error':
                                raise RuntimeError('wetype_stream_error')
                            if item.get('type') != 'snapshot':
                                raise ValueError('invalid_upstream_message')
                            revision, text, final = item.get('revision'), item.get('text'), item.get('final')
                            if type(revision) is not int or revision <= last_revision or not isinstance(text, str) or type(final) is not bool:
                                raise ValueError('invalid_upstream_snapshot')
                            if final and not ended.is_set():
                                raise ValueError('premature_upstream_final')
                            last_revision = revision
                            outcome.update(text=text, revisions=revision, final=final)
                            await send_snapshot(ws, sid, text, revision, final)
                            if final:
                                return
                    sender = asyncio.create_task(send_audio())
                    receiver = asyncio.create_task(receive_results())
                    try:
                        done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                        if receiver in done and not sender.done():
                            raise ValueError('premature_upstream_completion')
                        await asyncio.wait_for(receiver, FINAL_TIMEOUT)
                        complete = True
                    finally:
                        if not complete:
                            try:
                                await asyncio.wait_for(upstream.send(json.dumps({'type': 'cancel'})), 1)
                            except Exception:
                                pass
                        for task in (sender, receiver):
                            task.cancel()
                        await asyncio.gather(sender, receiver, return_exceptions=True)
    finally:
        outcome['elapsed_ms'] = round((time.monotonic()-started)*1000, 1)


@app.websocket('/v1')
async def recognize(ws: WebSocket):
    global active_sessions
    config = json.loads(CONFIG.read_text())
    try:
        validate_auth(ws, config)
    except Exception:
        await ws.close(code=1008)
        return
    if active_sessions >= 4:
        await ws.close(code=1013)
        return
    active_sessions += 1
    sid = 'local-wetype-' + secrets.token_hex(12)
    accepted = False
    pcm, frames = bytearray(), []
    params = record = None
    uploaded_at = previous_at = time.monotonic()
    end_received = False
    failure = None
    stream_task = upload_task = disconnect_task = None
    chunks = asyncio.Queue(maxsize=64)
    outcome = {}
    async def stop_work():
        tasks = tuple(t for t in (upload_task,stream_task,disconnect_task) if t)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    try:
        await ws.accept()
        accepted = True
        async def receive_audio():
            nonlocal params, previous_at, end_received, stream_task
            first, last_seq = True, -1
            deadline = uploaded_at + UPLOAD_TIMEOUT
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise ValueError('audio_upload_timeout')
                data = json.loads(await asyncio.wait_for(ws.receive_text(), min(FRAME_TIMEOUT, remaining)))
                header = data.get('header', {})
                audio = data.get('payload', {}).get('audio', {})
                status = audio.get('status')
                if type(status) is not int or status not in (0, 1, 2) or header.get('status') != status:
                    raise ValueError('invalid_status')
                if first:
                    if status != 0 or header.get('app_id') != config['iat']['app_id']:
                        raise ValueError('invalid_first_frame_or_app_id')
                    iat = data.get('parameter', {}).get('iat', {})
                    if iat.get('domain', 'spark_asr_v2') != 'spark_asr_v2':
                        raise ValueError('unsupported_domain')
                    if any(iat.get('result', {}).get(k, v) != v for k,v in (('compress','raw'),('encoding','utf8'),('format','json'))):
                        raise ValueError('unsupported_result_format')
                    if any(iat.get(k) for k in ('vinfo','dhw','opt','ptt','smth','nunum','rlang','vto','eos')):
                        raise ValueError('unsupported_recognition_option')
                    params = (audio.get('sample_rate',16000), audio.get('channels',1), audio.get('bit_depth',16))
                    if params[0] not in (8000,16000) or params[1] not in (1,2) or params[2] not in (8,16):
                        raise ValueError('unsupported_pcm_format')
                    await ws.send_json({'header': {'code':0,'message':'success','sid':sid,'status':0}})
                elif status == 0:
                    raise ValueError('duplicate_first_frame')
                if audio.get('encoding','raw') != 'raw':
                    raise ValueError('only_raw_pcm_supported')
                if any(k in audio and audio[k] != value for k,value in zip(('sample_rate','channels','bit_depth'),params)):
                    raise ValueError('audio_format_changed')
                seq = audio.get('seq')
                if type(seq) is not int or seq <= last_seq:
                    raise ValueError('invalid_sequence')
                last_seq = seq
                chunk = base64.b64decode(audio.get('audio',''), validate=True)
                if len(chunk) % (params[1]*(params[2]//8)):
                    raise ValueError('unaligned_pcm_frame')
                if len(pcm)+len(chunk) > min(10*1024*1024, params[0]*params[1]*(params[2]//8)*MAX_AUDIO_SECONDS):
                    raise ValueError('audio_exceeds_60_seconds')
                current_at = time.monotonic()
                if len(frames)<10000:
                    frames.append({'seq':seq,'status':status,'bytes':len(chunk),
                                   'ms':round((current_at-uploaded_at)*1000,1),'gap_ms':round((current_at-previous_at)*1000,1)})
                previous_at = current_at
                pcm.extend(chunk)
                if params == (16000,1,16) and chunk:
                    if stream_task is None:
                        stream_task = asyncio.create_task(stream_native(config,chunks,ws,sid,outcome))
                    await asyncio.wait_for(chunks.put(chunk), max(0.01, deadline-time.monotonic()))
                first = False
                if status == 2:
                    end_received = True
                    if not pcm:
                        raise ValueError('empty_or_unaligned_pcm')
                    if stream_task:
                        await asyncio.wait_for(chunks.put(None), max(0.01,deadline-time.monotonic()))
                    return
        upload_task = asyncio.create_task(receive_audio())
        # The first frame decides streaming vs compatibility mode.
        while not upload_task.done() and stream_task is None:
            await asyncio.wait((upload_task,), timeout=0.01)
        if stream_task:
            done, _ = await asyncio.wait((upload_task,stream_task), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if stream_task in done and not upload_task.done():
                raise ValueError('upstream_finished_before_audio_end')
        await upload_task
        data = wav_bytes(pcm,params)
        record = await archive(data,'audio.wav','xfyun-websocket',
            {'sid':sid,'upload_ms':round((time.monotonic()-uploaded_at)*1000,1),
             'end_frame_received':end_received,'frames':frames,'pcm_bytes':len(pcm),
             'transport':'native-websocket' if stream_task else 'buffered-http'})
        async def watch_disconnect():
            # Stay responsive to disconnect even after the client has sent end.
            message = await ws.receive()
            if message.get('type') == 'websocket.disconnect':
                raise WebSocketDisconnect(message.get('code',1000))
            raise ValueError('frame_after_audio_end')
        disconnect_task = asyncio.create_task(watch_disconnect())
        async def finish():
            if stream_task:
                await stream_task
                await update(record,state='completed',result={'text':outcome.get('text','')},completion='native-end-flag',**outcome)
            else:
                response = await forward(record,config,data)
                response.raise_for_status()
                await send_snapshot(ws,sid,response.json()['text'],0,True)
        finish_task = asyncio.create_task(finish())
        try:
            done,_ = await asyncio.wait((finish_task,disconnect_task),return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            await finish_task
        finally:
            finish_task.cancel()
            await asyncio.gather(finish_task,return_exceptions=True)
    except WebSocketDisconnect:
        failure = 'client_disconnected'
    except (ValueError,TypeError,KeyError,asyncio.TimeoutError) as exc:
        failure = type(exc).__name__+': '+str(exc)[:120]
        await stop_work()
        if accepted:
            try:
                await ws.send_json({'header':{'code':10160,'message':str(exc)[:100] or 'audio_or_upstream_timeout','sid':sid,'status':2}})
            except Exception:
                pass
    except Exception as exc:
        failure = type(exc).__name__
        await stop_work()
        if accepted:
            try:
                await ws.send_json({'header':{'code':10110,'message':'wetype_upstream_unavailable','sid':sid,'status':2}})
            except Exception:
                pass
    finally:
        await stop_work()
        try:
            if record is None and pcm and params:
                record = await archive(wav_bytes(pcm,params),'partial.wav','xfyun-websocket',
                    {'sid':sid,'end_frame_received':end_received,'frames':frames,'pcm_bytes':len(pcm),
                     'upload_ms':round((time.monotonic()-uploaded_at)*1000,1)})
                await update(record,state='incomplete',error=failure,**outcome)
            elif record and failure:
                await update(record,state='failed',websocket_error=failure,**outcome)
        except Exception:
            pass
        active_sessions -= 1
        if accepted:
            try:
                await ws.close()
            except Exception:
                pass
