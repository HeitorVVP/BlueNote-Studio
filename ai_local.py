"""BlueNote Studio — servidor local experimental de transcrição com IA.

Uso: python ai_local.py
Padrão: 127.0.0.1:8765 (somente seu computador).
Depende da instalação opcional de: basic-pitch, demucs, librosa, pretty_midi,
ffmpeg (programa disponível no PATH). A instalação inicial requer internet.

AVISO: saídas MIDI são estimativas, NÃO a transcrição perfeita de todos os
instrumentos de uma música. Não use o serviço em redes públicas.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HOST = "127.0.0.1"
PORT = 8765
MAX_BYTES = 90 * 1024 * 1024
MAX_SECONDS = 120
_busy = threading.Lock()


def available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def ffmpeg_wav(source: Path, dest: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg não encontrado. Instale-o e inclua no PATH.")
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(source), "-t", str(MAX_SECONDS), "-vn", "-ac", "2",
           "-ar", "44100", str(dest)]
    subprocess.run(cmd, check=True, timeout=90, capture_output=True)


def separated_sources(wav_path: Path, output: Path) -> dict[str, Path]:
    """Tenta separar em 4 fontes; falha de Demucs mantém uma pista geral."""
    if not available("demucs"):
        print("[BlueNote] Demucs ausente, analisando áudio inteiro.", flush=True)
        return {"other": wav_path}
    cmd = [sys.executable, "-m", "demucs", "-n", "htdemucs", "-o", str(output), str(wav_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode:
            print("[BlueNote] Demucs falhou; usando áudio inteiro:", result.stderr[-500:], flush=True)
            return {"other": wav_path}
        stems_dir = output / "htdemucs" / wav_path.stem
        stems = {name: stems_dir / (name + ".wav") for name in ("vocals", "bass", "other", "drums")}
        if not all(p.is_file() for p in stems.values()):
            raise RuntimeError("Arquivos dos stems não encontrados")
        return stems
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
        print("[BlueNote] Demucs indisponível:", exc, flush=True)
        return {"other": wav_path}


def generate_midi(wav_path: Path, workdir: Path) -> bytes:
    if not available("basic_pitch") or not available("pretty_midi"):
        raise RuntimeError("Instale primeiro: python -m pip install basic-pitch pretty_midi")

    import pretty_midi
    from basic_pitch.inference import predict
    import numpy as np

    stems = separated_sources(wav_path, workdir / "stems")
    base = pretty_midi.PrettyMIDI(initial_tempo=120)
    programs = {"vocals": 53, "bass": 33, "other": 0}

    # Detectar BPM em separado: se librosa não estiver disponível, usa 120 BPM.
    try:
        import librosa
        y, sr = librosa.load(str(wav_path), sr=11025, mono=True, duration=MAX_SECONDS)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr, trim=False)
        bpm = float(np.asarray(tempo).ravel()[0])
        if np.isfinite(bpm) and 45 < bpm < 230:
            base = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    except Exception as exc:
        print("[BlueNote] BPM automático indisponível:", exc, flush=True)

    for name, stem_path in stems.items():
        if name == "drums":
            continue
        print(f"[BlueNote] Transcrevendo {name}...", flush=True)
        kw = {"minimum_frequency": 45, "maximum_frequency": 350} if name == "bass" else {}
        _, midi_data, _ = predict(str(stem_path), **kw)
        notes = [note for ins in midi_data.instruments for note in ins.notes]
        if not notes:
            continue
        inst = pretty_midi.Instrument(program=programs.get(name, 0), name={
            "vocals": "Vocal (estimado)", "bass": "Baixo (estimado)",
            "other": "Outros instrumentos (misturados)"}[name])
        for note in notes:
            inst.notes.append(pretty_midi.Note(
                velocity=max(1, min(127, note.velocity)),
                pitch=max(0, min(127, note.pitch)),
                start=max(0.0, float(note.start)),
                end=max(float(note.start) + .03, float(note.end))))
        base.instruments.append(inst)

    # Bateria: estimativa de transientes, NÃO identificação de instrumentos de percussão real.
    if "drums" in stems and available("librosa"):
        try:
            import librosa
            y, sr = librosa.load(str(stems["drums"]), sr=22050, mono=True, duration=MAX_SECONDS)
            times = librosa.onset.onset_detect(y=y, sr=sr, hop_length=512, backtrack=False, units="time")
            percussion = pretty_midi.Instrument(program=0, is_drum=True, name="Bateria (estimada)")
            for t in times:
                segment = y[int(max(0, t-.025)*sr):int(min(len(y), (t+.08)*sr))]
                if len(segment) < 128:
                    continue
                spectrum = np.abs(np.fft.rfft(segment))
                freqs = np.fft.rfftfreq(len(segment), 1/sr)
                lo = spectrum[freqs < 140].sum()
                hi = spectrum[freqs > 2000].sum()
                pitch = 36 if lo > hi else 42 if hi > lo*3 else 38
                percussion.notes.append(pretty_midi.Note(velocity=95, pitch=pitch, start=float(t), end=float(t)+.12))
            if percussion.notes:
                base.instruments.append(percussion)
        except Exception as exc:
            print("[BlueNote] Detecção de bateria falhou:", exc, flush=True)

    if not any(i.notes for i in base.instruments):
        raise RuntimeError("O modelo não detectou notas suficientes no áudio.")
    out = workdir / "transcrito.mid"
    base.write(str(out))
    return out.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, content: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self) -> None:
        self.send(204, b"", "text/plain")

    def do_GET(self) -> None:
        if self.path == "/api/health":
            body = {"ok": True, "description": "Servidor BlueNote Studio local",
                    "basic_pitch": available("basic_pitch"),
                    "demucs": available("demucs"), "ffmpeg": shutil.which("ffmpeg") is not None}
            self.send(200, json.dumps(body).encode(), "application/json; charset=utf-8")
        else:
            self.send(404, b"Endpoint desconhecido", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/transcribe":
            self.send(404, b"Rota desconhecida", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not (0 < length <= MAX_BYTES):
            self.send(413, "Arquivo vazio ou grande demais (máximo de 90 MB).".encode(), "text/plain; charset=utf-8")
            return
        if not _busy.acquire(blocking=False):
            self.send(429, "Já existe uma análise em andamento.".encode(), "text/plain; charset=utf-8")
            return
        try:
            name = Path(unquote(self.headers.get("X-Filename", "audio.wav"))).name
            ext = Path(name).suffix.lower()
            if ext not in (".mp3", ".wav", ".m4a", ".mp4", ".ogg", ".webm", ".flac"):
                raise RuntimeError("Extensão de arquivo de áudio não suportada")
            with tempfile.TemporaryDirectory(prefix="bluenote_") as tmp:
                work = Path(tmp)
                source = work / ("entrada" + ext)
                with source.open("wb") as f:
                    remains = length
                    while remains:
                        chunk = self.rfile.read(min(65536, remains))
                        if not chunk:
                            raise RuntimeError("Transferência incompleta")
                        f.write(chunk)
                        remains -= len(chunk)
                wav = work / "audio.wav"
                ffmpeg_wav(source, wav)
                result = generate_midi(wav, work)
                self.send(200, result, "audio/midi")
        except Exception as exc:
            print("[BlueNote] Erro:", repr(exc), flush=True)
            try:
                self.send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            _busy.release()


if __name__ == "__main__":
    print(f"BlueNote IA local: http://{HOST}:{PORT}")
    print("O processamento é experimental e depende dos modelos instalados.")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Encerrado.")
