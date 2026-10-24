"""Transcribe a PCM WAV through the local Xfyun-format endpoint.

Usage on Ubuntu: python scripts/iat_client.py recording.wav
Credentials are read from the private server configuration, never logged.
"""
import asyncio
import base64
from email.utils import formatdate
import hashlib
import hmac
import json
from pathlib import Path
import sys
from urllib.parse import urlencode, urlsplit
import wave

import websockets


async def main():
    config_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('data/config.json')
    config = json.loads(config_path.read_text())
    with wave.open(sys.argv[1], 'rb') as recording:
        rate, channels, width = recording.getframerate(), recording.getnchannels(), recording.getsampwidth()
        if rate not in (8000, 16000) or channels not in (1, 2) or width not in (1, 2):
            raise ValueError('Use 8/16 kHz PCM WAV, 1/2 channels, 8/16 bit')
        if not 0 < recording.getnframes() / rate <= 60:
            raise ValueError('Recording must be longer than 0 and at most 60 seconds')
        pcm = recording.readframes(recording.getnframes())
    endpoint = config['iat_url']
    parts = urlsplit(endpoint)
    host, path = parts.netloc, parts.path
    date = formatdate(usegmt=True)
    origin = f'host: {host}\ndate: {date}\nGET {path} HTTP/1.1'
    signature = base64.b64encode(hmac.new(config['iat']['api_secret'].encode(), origin.encode(), hashlib.sha256).digest()).decode()
    authorization = f'api_key="{config["iat"]["api_key"]}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
    url = endpoint + '?' + urlencode({'host': host, 'date': date, 'authorization': base64.b64encode(authorization.encode()).decode()})
    async with websockets.connect(url, proxy=None) as ws:
        size = rate * channels * width // 25  # 40 ms chunks; file uploads can be faster than real time.
        seq = 0
        for offset in range(0, len(pcm), size):
            status = 0 if seq == 0 else 1
            seq += 1
            frame = {'header': {'app_id': config['iat']['app_id'], 'status': status},
                     'payload': {'audio': {'encoding': 'raw', 'sample_rate': rate, 'channels': channels,
                     'bit_depth': width * 8, 'seq': seq, 'status': status,
                     'audio': base64.b64encode(pcm[offset:offset + size]).decode()}}}
            if status == 0:
                frame['parameter'] = {'iat': {'domain': 'spark_asr_v2', 'language': 'zh_cn', 'accent': 'mandarin',
                    'result': {'encoding': 'utf8', 'compress': 'raw', 'format': 'json'}}}
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps({'header': {'status': 2}, 'payload': {'audio': {'status': 2, 'seq': seq + 1, 'audio': ''}}}))
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), 245))
            if data['header']['code']:
                raise RuntimeError(data['header']['message'])
            if data['header']['status'] == 2:
                result = json.loads(base64.b64decode(data['payload']['result']['text']))
                print(''.join(x['w'] for word in result['ws'] for x in word['cw']))
                break


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        # Connection exceptions may contain signed URLs; do not expose them.
        print('ASR request failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
