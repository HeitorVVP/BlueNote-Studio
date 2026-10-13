"""Testes sem downloads reais; run: python testes_local.py"""
import io
import os
import importlib.util
import pathlib
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
import json

ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('server',ROOT/'servidor_online.py')
server=importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

class FakeProc:
    def __init__(self, lines, code, cmd, outfile=None):
        self.stdout=io.StringIO(''.join(x+'\n' for x in lines))
        self.code=code
        self.cmd=cmd
        if outfile:
            outfile.write_bytes(b'ID3' + b'\0' * 4096)
    def wait(self, timeout=None): return self.code
    def terminate(self): self.code=-1
    def kill(self): self.code=-9

class Tests(unittest.TestCase):
    def test_profiles(self):
        self.assertTrue(server.is_youtube('https://youtu.be/aqz-KE-bpKQ'))
        self.assertTrue(server.is_youtube('https://www.youtube.com/watch?v=a'))
        self.assertFalse(server.is_youtube('https://youtube.com.example.net/watch'))
        j={'quality':192,'url':'https://www.youtube.com/watch?v=aqz-KE-bpKQ'}
        a=server.build_command(j, pathlib.Path('/tmp/abc'))
        b=server.build_command(j, pathlib.Path('/tmp/abc'), alternative=True)
        self.assertIn('--js-runtimes',a)
        self.assertIn('--remote-components',b)
        self.assertIn('youtube:player_client=web_safari,android_vr',b)
        self.assertEqual(a[-2],'--')
        self.assertEqual(a[-1],j['url'])

    def test_retry_on_bot_block_and_success(self):
        j=dict(id='b12345678901234567890123',quality=192,url='https://youtu.be/aqz-KE-bpKQ',
               cancelled=False,created=time.time(),status='queued', title='')
        calls=[]
        def fake_popen(cmd,**kw):
            calls.append(cmd)
            if len(calls)==1:
                return FakeProc(['ERROR: Sign in to confirm you are not a bot'],1,cmd)
            return FakeProc(['BLUENOTE_TITLE:Demo','[download] 100.0%', '[ExtractAudio] Destination'],0,cmd,server.STORE/j['id']/'audio.mp3')
        with patch.object(server.subprocess,'Popen',side_effect=fake_popen):
            server.run_job(j)
        self.assertEqual(len(calls),2)
        self.assertEqual(j['status'],'ready')
        self.assertEqual(j['progress'],100)
        self.assertEqual(j['filename'],'Demo.mp3')
        (server.STORE/j['id']/'audio.mp3').unlink(missing_ok=True)
        (server.STORE/j['id']).rmdir()

    def test_stop_on_unavailable(self):
        j=dict(id='a12345678901234567890123',quality=192,url='https://youtu.be/aqz-KE-bpKQ',
               cancelled=False,created=time.time(),status='queued', title='')
        n=[]
        def popen(cmd,**kw):
            n.append(1)
            return FakeProc(['ERROR: Video unavailable'],1,cmd)
        with patch.object(server.subprocess,'Popen',side_effect=popen):
            server.run_job(j)
        self.assertEqual(len(n),1)
        self.assertEqual(j['status'],'error')
        self.assertIn('indisponível',j['error'])

    def test_no_more_than_two_bot_retries(self):
        j=dict(id='c12345678901234567890123',quality=192,url='https://youtu.be/aqz-KE-bpKQ',
               cancelled=False,created=time.time(),status='queued', title='')
        calls=[]
        def popen(cmd,**kw):
            calls.append(1)
            return FakeProc(['ERROR: Sign in to confirm you are not a bot'],1,cmd)
        with patch.object(server.subprocess,'Popen',side_effect=popen):
            server.run_job(j)
        self.assertEqual(len(calls),2)
        self.assertEqual(j['status'],'error')
        self.assertIn('anti-bot',j['error'])

    def test_http_auth_and_health(self):
        with patch.object(server,'KEY','my-long-test-key-1234567'):
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            th=threading.Thread(target=http.serve_forever,daemon=True)
            th.start()
            base='http://127.0.0.1:'+str(http.server_port)
            try:
                with urlopen(base+'/api/health',timeout=4) as r:
                    h=json.loads(r.read())
                self.assertEqual(h['version'],'1.4')
                self.assertTrue(h['youtube_fallback'])
                with urlopen(Request(base+'/api/auth',headers={'X-BlueNote-Key':'my-long-test-key-1234567'}),timeout=4) as r:
                    a=json.loads(r.read())
                self.assertTrue(a['ok'])
            finally:
                http.shutdown(); http.server_close(); th.join(timeout=4)

if __name__=='__main__':unittest.main(verbosity=2)
