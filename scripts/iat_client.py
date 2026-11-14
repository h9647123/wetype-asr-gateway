"""Transcribe a PCM WAV through the local Xfyun-format endpoint.

Usage on Ubuntu: python scripts/iat_client.py recording.wav
Credentials are read from the private server configuration, never logged.
"""
import asyncio
import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav', type=Path)
    parser.add_argument('config', type=Path, nargs='?', default=Path('data/config.json'))
    parser.add_argument('--realtime', action='store_true', help='Pace prerecorded PCM as if recorded live')
    parser.add_argument('--partials', action='store_true', help='Print revisable snapshot text before final (private transcript)')
    args = parser.parse_args()
    config_path = args.config
    config = json.loads(config_path.read_text())
    with wave.open(str(args.wav), 'rb') as recording:
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
        async def receive():
            while True:
                data = json.loads(await asyncio.wait_for(ws.recv(), 245))
                if data['header']['code']:
                    raise RuntimeError('ASR server returned an error')
                item = data.get('payload', {}).get('result', {})
                if item.get('text'):
                    result = json.loads(base64.b64decode(item['text']))
                    text = ''.join(word['cw'][0]['w'] for word in result['ws'])
                    if data['header']['status'] == 2:
                        print(text)
                        return
                    if args.partials:
                        print('[snapshot] ' + text, flush=True)
        receiver = asyncio.create_task(receive())
        try:
            await send_pcm(ws, config, pcm, rate, channels, width, args.realtime, receiver)
            await receiver
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


async def send_pcm(ws, config, pcm, rate, channels, width, realtime, receiver):
    size = rate * channels * width // 5  # 200 ms packets, while results are consumed concurrently.
    seq = 0
    started = asyncio.get_running_loop().time()
    for offset in range(0, len(pcm), size):
        if receiver.done():
            receiver.result()
            raise RuntimeError('ASR completed before all audio was sent')
        if realtime:
            ready = min(len(pcm), offset + size) / (rate * channels * width)
            await asyncio.sleep(max(0, ready - (asyncio.get_running_loop().time() - started)))
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


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        # Connection exceptions may contain signed URLs; do not expose them.
        print('ASR request failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
