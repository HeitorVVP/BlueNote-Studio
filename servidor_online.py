#!/usr/bin/env python3
"""BlueNote Studio 1.3: servidor web de uso pessoal para converter links públicos em MP3.
Requer APP_KEY forte, yt-dlp[default], FFmpeg e Deno. Inclui controles básicos
para exposição à internet; uso pessoal/autorizado apenas.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
STORE = pathlib.Path(tempfile.gettempdir()) / 'bluenote_cloud_v13'
STORE.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get('PORT', '10000'))
KEY = os.environ.get('APP_KEY', '').strip()
LOCK = threading.RLock()
JOBS = {}
RECENT = deque()
MAX_JSON = 8192
MAX_ACTIVE = 1
MAX_AGE = 25 * 60
MAX_LENGTH = 30 * 60
MAX_BYTES = 85 * 1024 * 1024
ALLOWED = {
    'youtube.com', 'youtu.be', 'youtube-nocookie.com',
    'soundcloud.com', 'vimeo.com', 'tiktok.com',
    'dailymotion.com', 'instagram.com', 'facebook.com',
}


def error_url(raw):
    if not isinstance(raw, str) or len(raw) > 2500:
        return 'Link inválido ou muito longo.'
    try:
        u = urllib.parse.urlsplit(raw.strip())
        if u.scheme != 'https' or not u.hostname or u.username or u.password:
            return 'Use um link https:// válido.'
        host = u.hostname.rstrip('.').lower()
        if not any(host == d or host.endswith('.' + d) for d in ALLOWED):
            return 'Site não habilitado para extração online. Use um link direto de áudio no navegador.'
        for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_global:
                return 'Endereço de rede não permitido.'
        return None
    except (ValueError, OSError):
        return 'Link inválido ou domínio indisponível.'


def public(job):
    return {k: job.get(k) for k in ('id', 'status', 'progress', 'message', 'title', 'filename', 'error')}


def gc():
    with LOCK:
        now = time.time()
        expired = [jid for jid, job in JOBS.items()
                   if job.get('status') not in ('working', 'queued')
                   and now - job['created'] > MAX_AGE]
        for jid in expired:
            JOBS.pop(jid, None)
            shutil.rmtree(STORE / jid, ignore_errors=True)
        while RECENT and now - RECENT[0] > 3600:
            RECENT.popleft()


def run_job(job):
    folder = STORE / job['id']
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
           '--no-warnings', '--newline', '--no-overwrites', '--socket-timeout', '18',
           '--retries', '2', '--fragment-retries', '2', '--max-filesize', '85M',
           '--match-filter', f'duration <= {MAX_LENGTH} & !is_live', '--restrict-filenames',
           '--no-write-thumbnail', '--no-write-info-json', '--no-write-comments',
           '-f', 'bestaudio/best', '-x', '--audio-format', 'mp3',
           '--audio-quality', f"{job['quality']}K", '--print',
           'after_move:BLUENOTE_TITLE:%(title)s',
           '-o', str(folder / 'audio.%(ext)s'), '--', job['url']]
    proc = None
    try:
        with LOCK:
            if job['cancelled']:
                job.update(status='cancelled', message='Cancelado.')
                return
            job.update(status='working', message='Conectando ao site…', progress=3)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', bufsize=1,
                                start_new_session=(os.name != 'nt'))
        with LOCK:
            job['proc'] = proc
        lines = []
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            lines = (lines + [line])[-7:]
            if line.startswith('BLUENOTE_TITLE:'):
                with LOCK:
                    job['title'] = line.split(':', 1)[1].strip()[:120]
            match = re.search(r'\[download\]\s+([\d.,]+)%', line)
            if match:
                try:
                    p = float(match.group(1).replace(',', '.'))
                    with LOCK:
                        job.update(progress=min(88, round(5 + .80 * p)), message='Baixando áudio…')
                except ValueError:
                    pass
            elif 'ExtractAudio' in line or 'Deleting original file' in line:
                with LOCK:
                    job.update(progress=92, message='Convertendo com FFmpeg…')
            if job['cancelled'] or (time.time() - job['created'] > 360):
                job['cancelled'] = True
                proc.terminate()
                break
        try:
            code = proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        if job['cancelled']:
            with LOCK:
                job.update(status='cancelled', progress=0, message='Cancelado ou excedeu o tempo máximo.')
            return
        files = list(folder.glob('*.mp3'))
        if code or not files:
            # Do not leak stdout logs or remote URL in errors to browsers.
            raise RuntimeError('O site recusou ou não disponibilizou este áudio. Tente outro vídeo público.')
        output = max(files, key=lambda p: p.stat().st_size)
        if output.stat().st_size > MAX_BYTES:
            raise RuntimeError('O arquivo ultrapassou 85 MB.')
        with LOCK:
            job.update(status='ready', progress=100, message='MP3 pronto!', path=str(output),
                       filename=(job.get('title') or 'Audio')[:120] + '.mp3')
    except Exception as exc:
        with LOCK:
            job.update(status='error', error=str(exc)[:250], message='Não foi possível concluir.')
    finally:
        with LOCK:
            job.pop('proc', None)
        if job['status'] != 'ready':
            shutil.rmtree(folder, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'BlueNoteCloud/1.3'
    def log_message(self, fmt, *args):
        # Omit URLs, referrers and keys from provider logs.
        return

    def common(self, mime, length=None, extra=None):
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-BlueNote-Key')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        if length is not None:
            self.send_header('Content-Length', str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def respond(self, code, data):
        buf = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.common('application/json; charset=utf-8', len(buf))
        self.wfile.write(buf)

    def authed(self):
        got = self.headers.get('X-BlueNote-Key', '')
        return len(KEY) >= 16 and bool(got) and hmac.compare_digest(KEY, got)

    def do_OPTIONS(self):
        self.send_response(204)
        self.common('text/plain', 0)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/index.html'):
            data = (HERE / 'index.html').read_bytes()
            self.send_response(200)
            self.common('text/html; charset=utf-8', len(data), {'Content-Security-Policy': "frame-ancestors 'none'"})
            self.wfile.write(data)
            return
        if path == '/api/health':
            return self.respond(200, {
                'ok': True, 'version': '1.3', 'requires_key': True,
                'configured': len(KEY) >= 16, 'yt_dlp': self.has_ytdlp(),
                'ffmpeg': bool(shutil.which('ffmpeg')),
                'js_runtime': 'deno' if shutil.which('deno') else None,
            })
        if not path.startswith('/api/'):
            return self.respond(404, {'error': 'Página não encontrada.'})
        if not self.authed():
            return self.respond(401, {'error': 'Senha do servidor incorreta ou ausente.'})
        if path == '/api/auth':
            return self.respond(200, {'ok': True})
        m = re.fullmatch(r'/api/jobs/([0-9a-f]{24})', path)
        if m:
            gc()
            with LOCK:
                job = JOBS.get(m.group(1))
                snap = public(job) if job else None
            return self.respond(200, snap) if snap else self.respond(404, {'error': 'Tarefa não encontrada.'})
        m = re.fullmatch(r'/api/(file|stream)/([0-9a-f]{24})', path)
        if m:
            with LOCK:
                job = JOBS.get(m.group(2))
                snap = dict(job) if job else None
            if not snap or snap['status'] != 'ready':
                return self.respond(404, {'error': 'MP3 indisponível.'})
            f = pathlib.Path(snap['path'])
            if not f.is_file() or f.stat().st_size > MAX_BYTES:
                return self.respond(404, {'error': 'MP3 expirou.'})
            self.send_response(200)
            self.common('audio/mpeg', f.stat().st_size,
                        {'Content-Disposition': 'attachment; filename="BlueNote.mp3"'} if m.group(1) == 'file' else None)
            try:
                with f.open('rb') as stream:
                    shutil.copyfileobj(stream, self.wfile, length=64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        return self.respond(404, {'error': 'Rota desconhecida.'})

    def has_ytdlp(self):
        try:
            import yt_dlp
            return True
        except ImportError:
            return False

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.authed():
            return self.respond(401, {'error': 'Senha do servidor incorreta ou ausente.'})
        if path not in ('/api/download',) and not re.fullmatch(r'/api/cancel/[0-9a-f]{24}', path):
            return self.respond(404, {'error': 'Rota desconhecida.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 2 or length > MAX_JSON:
                return self.respond(413, {'error': 'Dados inválidos ou muito grandes.'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('body not object')
        except (ValueError, UnicodeDecodeError):
            return self.respond(400, {'error': 'JSON inválido.'})
        if path == '/api/download':
            why = error_url(body.get('url'))
            if why:
                return self.respond(400, {'error': why})
            if not self.has_ytdlp() or not shutil.which('ffmpeg') or not shutil.which('deno'):
                return self.respond(503, {'error': 'O servidor ainda não tem yt-dlp, FFmpeg e Deno instalados.'})
            quality = body.get('quality', 192)
            if quality not in (128, 192, 256, 320):
                return self.respond(400, {'error': 'Qualidade inválida.'})
            gc()
            with LOCK:
                if sum(j['status'] in ('queued', 'working') for j in JOBS.values()) >= MAX_ACTIVE:
                    return self.respond(429, {'error': 'Já existe uma conversão em andamento. Aguarde.'})
                if len(RECENT) >= 10:
                    return self.respond(429, {'error': 'Limite de 10 conversões por hora. Tente mais tarde.'})
                jid = uuid.uuid4().hex[:24]
                job = dict(id=jid, url=body['url'].strip(), quality=quality,
                           status='queued', progress=0, message='Aguardando…', title='',
                           filename='', error='', cancelled=False, created=time.time())
                JOBS[jid] = job
                RECENT.append(time.time())
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            return self.respond(202, public(job))
        m = re.fullmatch(r'/api/cancel/([0-9a-f]{24})', path)
        if m:
            with LOCK:
                job = JOBS.get(m.group(1))
                if not job:
                    return self.respond(404, {'error': 'Tarefa não encontrada.'})
                if job['status'] not in ('queued', 'working'):
                    return self.respond(409, {'error': 'Tarefa já finalizada.'})
                job['cancelled'] = True
                proc = job.get('proc')
            if proc:
                try:
                    proc.terminate()
                except OSError:
                    pass
            return self.respond(200, {'ok': True})
        return self.respond(404, {'error': 'Rota desconhecida.'})


if __name__ == '__main__':
    if len(KEY) < 16:
        print('ERRO: defina APP_KEY (senha forte, pelo menos 16 caracteres) nas variáveis do serviço.', flush=True)
        sys.exit(2)
    print(f'BlueNote online v1.3 em 0.0.0.0:{PORT}, com autenticação.', flush=True)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
