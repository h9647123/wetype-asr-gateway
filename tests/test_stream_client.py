"""Public CLI uses duplex WebSocket and keeps credentials out of stdout."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import wave

from websockets.asyncio.server import serve


def test_prerecorded_cli_consumes_partial_and_final(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('public_iat_client', Path('scripts/iat_client.py'))
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    wav = tmp_path / 'fixture.wav'
    with wave.open(str(wav), 'wb') as output:
        output.setframerate(16000)
        output.setnchannels(1)
        output.setsampwidth(2)
        output.writeframes(b'\x00\x00' * 9600)
    config = tmp_path / 'private.json'
    observed = []

    async def run():
        async def native(ws):
            # This server emulates the external IAT contract, not native ASR.
            while True:
                frame = json.loads(await ws.recv())
                observed.append(frame)
                status = frame['header']['status']
                words = {'sn': 0, 'pgs': 'rpl', 'rg': [0, 0],
                         'ws': [{'cw': [{'w': '最终全文' if status == 2 else '当前草稿'}]}]}
                encoded = base64.b64encode(json.dumps(words).encode()).decode()
                await ws.send(json.dumps({'header': {'code': 0, 'status': 2 if status == 2 else 1},
                                          'payload': {'result': {'text': encoded}}}))
                if status == 2:
                    return
        async with serve(native, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            config.write_text(json.dumps({'iat_url': f'ws://127.0.0.1:{port}/v1',
                'iat': {'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret', 'app_id': 'test'}}))
            monkeypatch.setattr(sys, 'argv', ['iat_client.py', str(wav), str(config), '--realtime', '--partials'])
            await asyncio.wait_for(client.main(), 3)
    asyncio.run(run())
    output = capsys.readouterr().out
    assert '[snapshot] 当前草稿' in output
    assert output.endswith('最终全文\n')
    assert 'synthetic-key' not in output and 'synthetic-secret' not in output
    assert [item['header']['status'] for item in observed] == [0, 1, 1, 2]
    assert len(b''.join(base64.b64decode(item['payload']['audio']['audio']) for item in observed)) == 19200

