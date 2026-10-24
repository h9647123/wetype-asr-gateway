"""Local Xfyun v2 wire-format adapter; buffers PCM and returns one final result.

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

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

CONFIG = Path(os.environ.get('WETYPE_CONFIG', str(Path(os.environ.get('WETYPE_DATA_DIR', './data')) / 'config.json')))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
backend_lock = asyncio.Semaphore(1)
active_sessions = 0


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
    return {'ok': True, 'protocol': 'xfyun-v2-compatible', 'streaming_results': False, 'audio_encoding': ['raw']}


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
    try:
        await ws.accept()
        accepted = True
        pcm = bytearray()
        params = None
        last_seq = -1
        first = True
        deadline = time.monotonic() + 90
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('audio_upload_timeout')
            data = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=min(15, remaining)))
            header = data.get('header', {})
            audio = data.get('payload', {}).get('audio', {})
            status = audio.get('status')
            if status not in (0, 1, 2) or header.get('status') != status:
                raise ValueError('invalid_status')
            if first:
                if status != 0 or header.get('app_id') != config['iat']['app_id']:
                    raise ValueError('invalid_first_frame_or_app_id')
                iat = data.get('parameter', {}).get('iat', {})
                if iat.get('domain', 'spark_asr_v2') != 'spark_asr_v2':
                    raise ValueError('unsupported_domain')
                if iat.get('result', {}).get('compress', 'raw') != 'raw' or iat.get('result', {}).get('encoding', 'utf8') != 'utf8' or iat.get('result', {}).get('format', 'json') != 'json':
                    raise ValueError('unsupported_result_format')
                # dwa is accepted for client compatibility; there are no partial results to revise.
                if any(iat.get(k) for k in ('vinfo', 'dhw', 'opt', 'ptt', 'smth', 'nunum', 'rlang', 'vto', 'eos')):
                    raise ValueError('unsupported_recognition_option')
                params = (audio.get('sample_rate', 16000), audio.get('channels', 1), audio.get('bit_depth', 16))
                if params[0] not in (8000, 16000) or params[1] not in (1, 2) or params[2] not in (8, 16):
                    raise ValueError('unsupported_pcm_format')
                await ws.send_json({'header': {'code': 0, 'message': 'success', 'sid': sid, 'status': 0}})
            elif status == 0:
                raise ValueError('duplicate_first_frame')
            if audio.get('encoding', 'raw') != 'raw':
                raise ValueError('only_raw_pcm_supported')
            if any(k in audio and audio[k] != value for k, value in zip(('sample_rate', 'channels', 'bit_depth'), params)):
                raise ValueError('audio_format_changed')
            seq = audio.get('seq')
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                raise ValueError('invalid_sequence')
            last_seq = seq
            chunk = base64.b64decode(audio.get('audio', ''), validate=True)
            pcm.extend(chunk)
            if len(pcm) > min(10 * 1024 * 1024, params[0] * params[1] * (params[2] // 8) * 60):
                raise ValueError('audio_exceeds_60_seconds')
            first = False
            if status == 2:
                break
        if not pcm or len(pcm) % (params[1] * (params[2] // 8)):
            raise ValueError('empty_or_unaligned_pcm')
        output = io.BytesIO()
        with wave.open(output, 'wb') as recording:
            recording.setframerate(params[0])
            recording.setnchannels(params[1])
            recording.setsampwidth(params[2] // 8)
            recording.writeframes(pcm)
        async with asyncio.timeout(240):
            async with backend_lock:
                async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                    response = await client.post(config['upstream'] + '/v1/audio/transcriptions',
                        headers={'Authorization': 'Bearer ' + config['api_key']},
                        data={'model': 'wetype-voice'}, files={'file': ('audio.wav', output.getvalue(), 'audio/wav')})
                    response.raise_for_status()
                    text = response.json()['text']
        result = {'ret': 0, 'sn': 0, 'ls': True, 'ws': [{'bg': 0, 'cw': [{'w': text}]}]}
        encoded = base64.b64encode(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()).decode()
        await ws.send_json({'header': {'code': 0, 'message': 'success', 'sid': sid, 'status': 2},
            'payload': {'result': {'compress': 'raw', 'encoding': 'utf8', 'format': 'json', 'seq': 0, 'status': 2, 'text': encoded}}})
    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError, KeyError, asyncio.TimeoutError) as exc:
        if accepted:
            try:
                await ws.send_json({'header': {'code': 10160, 'message': str(exc)[:100], 'sid': sid, 'status': 2}})
            except Exception:
                pass
    except Exception:
        if accepted:
            try:
                await ws.send_json({'header': {'code': 10110, 'message': 'wetype_upstream_unavailable', 'sid': sid, 'status': 2}})
            except Exception:
                pass
    finally:
        active_sessions -= 1
        if accepted:
            try:
                await ws.close()
            except Exception:
                pass
