"""Isolated async gateway contract tests. No native recognition or server writes."""
import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gateway import iat as gateway
from fastapi import WebSocketDisconnect

CONFIG = {'api_key':'synthetic-test-only','upstream':'http://127.0.0.1:9999',
          'iat':{'api_key':'test','api_secret':'secret','app_id':'app'}}

class Board:
    def __init__(self):
        self.incoming=asyncio.Queue(); self.outgoing=[]; self.closed=False
        date=formatdate(usegmt=True); host='localhost'
        canonical=f'host: {host}\ndate: {date}\nGET /v1 HTTP/1.1'
        signature=base64.b64encode(hmac.new(b'secret',canonical.encode(),hashlib.sha256).digest()).decode()
        auth=f'api_key="test", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
        self.query_params={'host':host,'date':date,'authorization':base64.b64encode(auth.encode()).decode()}
        self.headers={'host':host}; self.url=SimpleNamespace(path='/v1')
    async def accept(self): pass
    async def close(self,code=1000): self.closed=True; self.close_code=code
    async def receive_text(self):
        item=await self.incoming.get()
        if item is None: raise WebSocketDisconnect(1000)
        return json.dumps(item)
    async def receive(self):
        item=await self.incoming.get()
        return {'type':'websocket.disconnect','code':1000} if item is None else {'type':'websocket.receive','text':json.dumps(item)}
    async def send_json(self,data): self.outgoing.append(data)
    def frame(self,status,seq,pcm=b'\x00\x00'*320,**audio):
        self.incoming.put_nowait({'header':{'app_id':'app','status':status},
            'parameter':{'iat':{}},'payload':{'audio':{'status':status,'seq':seq,
            'audio':base64.b64encode(pcm).decode(),**audio}}})

class Native:
    def __init__(self,stall=False):
        self.messages=asyncio.Queue(); self.sent=[]; self.closed=False; self.revision=0; self.stall=stall
        self.messages.put_nowait(json.dumps({'type':'ready','sample_rate':16000,'channels':1,'format':'s16le'}))
    async def __aenter__(self): return self
    async def __aexit__(self,*args): self.closed=True
    async def recv(self): return await self.messages.get()
    async def send(self,data):
        self.sent.append(data)
        if isinstance(data,bytes):
            self.revision+=1
            self.messages.put_nowait(json.dumps({'type':'snapshot','revision':self.revision,'text':f'partial{self.revision}','final':False}))
        elif json.loads(data)['type']=='end' and not self.stall:
            self.revision+=1
            self.messages.put_nowait(json.dumps({'type':'snapshot','revision':self.revision,'text':'final text','final':True}))

async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate(): await asyncio.sleep(.002)

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.config=Path(self.temp.name)/'config.json'
        self.config.write_text(json.dumps(CONFIG)); self.native=Native(); self.records=[]; self.updates=[]; self.connects=0
        async def archive(data,*args):
            record=(None,{'bytes':len(data)}); self.records.append(record); return record
        async def update(record,**values): self.updates.append(values)
        def connect(*args,**kwargs): self.connects+=1; return self.native
        self.patches=[patch.object(gateway,'CONFIG',self.config),patch.object(gateway,'archive',archive),
                      patch.object(gateway,'update',update),patch.object(gateway,'connect',connect),
                      patch.object(gateway,'backend_lock',asyncio.Semaphore(1))]
        for p in self.patches: p.start()
        self.board=Board(); self.task=asyncio.create_task(gateway.recognize(self.board))
    async def asyncTearDown(self):
        if not self.task.done():
            self.board.incoming.put_nowait(None)
            await asyncio.wait_for(self.task,2)
        for p in self.patches: p.stop()
        self.temp.cleanup()
        self.assertEqual(gateway.active_sessions,0)
    async def test_partial_before_end_and_final_replace(self):
        self.board.frame(0,0)
        await until(lambda:len(self.board.outgoing)>=2)
        self.assertFalse(self.task.done())
        partial=self.board.outgoing[1]; result=json.loads(base64.b64decode(partial['payload']['result']['text']))
        self.assertEqual(result['pgs'],'rpl'); self.assertEqual(result['rg'],[0,0]); self.assertFalse(result['ls'])
        self.board.frame(2,1,b'')
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.board.outgoing[-1]['header']['status'],2)
        self.assertEqual(self.updates[-1]['state'],'completed'); self.assertTrue(self.native.closed)
        self.assertEqual(sum(isinstance(x,bytes) for x in self.native.sent),1)
    async def test_out_of_order_cancels_no_http_replay(self):
        self.board.frame(0,3); await until(lambda:len(self.board.outgoing)>=2)
        self.board.frame(1,3)
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'invalid_sequence')
        self.assertIn({'type':'cancel'},[json.loads(x) for x in self.native.sent if isinstance(x,str)])
        self.assertTrue(self.native.closed); self.assertEqual(self.connects,1)
    async def test_shorter_snapshot_keeps_one_replacement_slot(self):
        self.board.frame(0,0)
        await until(lambda:len(self.board.outgoing)>=2)
        self.native.revision+=1
        self.native.messages.put_nowait(json.dumps({'type':'snapshot','revision':self.native.revision,
            'text':'p','final':False}))
        await until(lambda:len(self.board.outgoing)>=3)
        result=json.loads(base64.b64decode(self.board.outgoing[-1]['payload']['result']['text']))
        self.assertEqual(result['sn'],0); self.assertEqual(result['pgs'],'rpl'); self.assertEqual(result['rg'],[0,0])
        self.assertEqual(result['ws'][0]['cw'][0]['w'],'p')
        self.board.frame(2,1,b'')
        await asyncio.wait_for(self.task,2)
    async def test_disconnect_during_recording_cleanup(self):
        self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
        self.board.incoming.put_nowait(None); await asyncio.wait_for(self.task,2)
        self.assertTrue(self.native.closed)
        self.assertEqual(self.updates[-1]['error'],'client_disconnected')
    async def test_disconnect_while_waiting_final_cleanup(self):
        self.native.stall=True
        self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
        self.board.frame(2,1,b''); await until(lambda:any(isinstance(x,str) and json.loads(x)['type']=='end' for x in self.native.sent))
        self.board.incoming.put_nowait(None); await asyncio.wait_for(self.task,2)
        self.assertTrue(self.native.closed); self.assertEqual(self.updates[-1]['state'],'failed')
    async def test_limit_before_forward(self):
        with patch.object(gateway,'MAX_AUDIO_SECONDS',.001):
            self.board.frame(0,0)
            await asyncio.wait_for(self.task,2)
        self.assertEqual(self.connects,0)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'audio_exceeds_60_seconds')
    async def test_invalid_first_no_upstream(self):
        self.board.frame(1,0)
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.connects,0)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'invalid_first_frame_or_app_id')
    async def test_pcm_compatibility_http_only(self):
        async def forward(record,config,data):
            class Response:
                def raise_for_status(self): pass
                def json(self): return {'text':'compatibility result'}
            return Response()
        with patch.object(gateway,'forward',forward):
            self.board.frame(0,0,sample_rate=8000)
            self.board.frame(2,1,b'')
            await asyncio.wait_for(self.task,2)
        self.assertEqual(self.connects,0)
        result=json.loads(base64.b64decode(self.board.outgoing[-1]['payload']['result']['text']))
        self.assertTrue(result['ls'])
    async def test_upstream_error_cleans_receiver(self):
        self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
        self.native.messages.put_nowait(json.dumps({'type':'error','message':'not copied into public response'}))
        await asyncio.wait_for(self.task,2)
        self.assertTrue(self.native.closed)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'wetype_upstream_unavailable')
    async def test_final_wait_is_bounded(self):
        self.native.stall=True
        with patch.object(gateway,'FINAL_TIMEOUT',.05):
            self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
            self.board.frame(2,1,b'')
            await asyncio.wait_for(self.task,2)
        self.assertTrue(self.native.closed)
        self.assertNotEqual(self.board.outgoing[-1]['header']['code'],0)
    async def test_format_change_cancels(self):
        self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
        self.board.frame(1,1,sample_rate=8000)
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'audio_format_changed')
        self.assertTrue(self.native.closed)
    async def test_frame_idle_timeout(self):
        with patch.object(gateway,'FRAME_TIMEOUT',.05):
            self.board.frame(0,0); await until(lambda:len(self.board.outgoing)>=2)
            await asyncio.wait_for(self.task,2)
        self.assertTrue(self.native.closed)
        self.assertEqual(self.board.outgoing[-1]['header']['code'],10160)
    async def test_odd_pcm_rejected_without_native(self):
        self.board.frame(0,0,b'odd')
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.connects,0)
        self.assertEqual(self.board.outgoing[-1]['header']['message'],'unaligned_pcm_frame')
    async def test_shared_backend_slot_serializes(self):
        await gateway.backend_lock.acquire()
        self.board.frame(0,0)
        await asyncio.sleep(.03)
        self.assertEqual(self.connects,0)
        gateway.backend_lock.release()
        await until(lambda:len(self.board.outgoing)>=2)
        self.board.frame(2,1,b'')
        await asyncio.wait_for(self.task,2)
    async def test_real_websocket_transport(self):
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
        async def handler(socket):
            self.assertEqual(socket.request.headers['Authorization'],'Bearer synthetic-test-only')
            await socket.send(json.dumps({'type':'ready','sample_rate':16000,'channels':1,'format':'s16le'}))
            self.assertIsInstance(await socket.recv(),bytes)
            await socket.send(json.dumps({'type':'snapshot','revision':0,'text':'real transport partial','final':False}))
            self.assertEqual(json.loads(await socket.recv()),{'type':'end'})
            await socket.send(json.dumps({'type':'snapshot','revision':1,'text':'real transport final','final':True}))
        async with serve(handler,'127.0.0.1',0) as server:
            port=server.sockets[0].getsockname()[1]
            with patch.object(gateway,'connect',connect),patch.object(gateway,'stream_url',lambda c:f'ws://127.0.0.1:{port}'):
                self.board.frame(0,0)
                await until(lambda:len(self.board.outgoing)>=2)
                self.board.frame(2,1,b'')
                await asyncio.wait_for(self.task,2)
        self.assertEqual(self.updates[-1]['text'],'real transport final')
    async def test_auth_reject(self):
        self.board.incoming.put_nowait(None)
        await asyncio.wait_for(self.task,2)
        self.board=Board()
        self.board.query_params['host']='attacker'
        self.task=asyncio.create_task(gateway.recognize(self.board))
        self.board.frame(0,0)
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.board.close_code,1008); self.assertEqual(self.connects,0)

if __name__=='__main__': unittest.main(verbosity=2)
