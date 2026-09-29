# -*- coding: utf-8 -*-
"""Klip Kalkanı: Düzenle sekmesi (Avidemux benzeri).

- Önizleme: sesli oynatma, zaman çubuğunda tıklayıp sürükleyerek gezinme, kare kare ilerleme.
- Kes: A (başlangıç) ve B (bitiş) işaretle, kaydet.
    * Kayıpsız: yeniden kodlamadan kopyalar (çok hızlı, kalite aynen). En yakın önceki anahtar kareden başlar.
    * Tam kare: sadece görüntüyü NVIDIA ekran kartıyla yeniden kodlar, tam A karesinden başlar.
- Ses kanalları: her kanal (Medal'de tüm ses, oyun, Discord, mikrofon) için aç/kapat, seviye (%0–200),
  tek dinle, seviye göstergesi ve istenen ad (hatırlanır, kaydedilen dosyaya da yazılır). Oynarken değiştirmek anında duyulur. Kaydederken ses olduğu gibi kopyalanır
  ya da ayarlanan seviyelerle tek kanalda birleştirilir / ayrı kanallar olarak yeniden kodlanır (AAC).
- Birleştir: aynı ayarlarla kaydedilmiş klipleri kayıpsız uç uca ekler.
Orijinal dosyalara hiç dokunulmaz; sonuç her zaman yeni bir dosyaya yazılır.
"""

import math
import os
import threading
import time
import tkinter as tk
import warnings
from fractions import Fraction
from tkinter import filedialog, messagebox, ttk  # noqa: F401

import av
from PIL import Image, ImageTk

try:
    import sounddevice as sd
except Exception:  # ses kütüphanesi yoksa önizleme sessiz çalışır
    sd = None
with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop  # ses karıştırma (C'de, hızlı); Python 3.12 ile gelir

VIDEO_TYPES = [('Video', '*.mp4 *.mkv *.mov *.webm *.avi'), ('Tüm dosyalar', '*.*')]
PREVIEW_W, PREVIEW_H = 800, 450
DISPLAY_FPS = 60  # varsayılan önizleme FPS'i (0 = klibin kendi FPS'i)
GREEN, RED, BLUE, GREY = '#22c55e', '#ef4444', '#3b82f6', '#9ca3af'
YELLOW = '#eab308'
MIX_RATE = 48000       # dışa aktarılan karışımın örnekleme hızı
MIX_BITRATE = 192000   # AAC
MAX_GAIN = 200         # seviye sürgüsünün sonu (%)
AUDIO_MODES = ['Olduğu gibi', 'Tek kanal (karışım)', 'Ayrı kanallar (ayarlı)']
# Medal kanal adları → Türkçe
TRACK_NAMES = {'all audio': 'Tüm ses', 'microphone': 'Mikrofon', 'mic': 'Mikrofon', 'discord': 'Discord',
               'desktop audio': 'Masaüstü sesi', 'system audio': 'Sistem sesi', 'game': 'Oyun'}


class Cancelled(Exception):
    pass


def fmt_time(t):
    t = max(0.0, t or 0.0)
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return (f'{h}:{m:02d}:{s:05.2f}' if h else f'{m:02d}:{s:05.2f}').replace('.', ',')


def fmt_size(n):
    for unit, div in (('GB', 1e9), ('MB', 1e6), ('KB', 1e3)):
        if n >= div:
            return f'{n / div:.2f} {unit}'.replace('.', ',')
    return f'{n} B'


def default_track_label(i, name, medal):
    """Kanalın varsayılan adı: Medal'in İngilizce adları Türkçe, oyun kanalı "Oyun (…)", adsızsa sıra numarası."""
    if name.lower() in TRACK_NAMES:
        return TRACK_NAMES[name.lower()]
    if name:
        return f'Oyun ({name})' if medal else name  # Medal'de adı süreç olan kanal oyunun sesi
    return f'Ses kanalı {i + 1}'


def probe(path, names=None):
    """Klip bilgisi: süre, çözünürlük, fps, kodek, ses kanalları. names: kullanıcının kanal adları
    {orijinal ad (adsızsa '#sıra'): verilen ad}."""
    with av.open(path) as c:
        v = c.streams.video[0] if c.streams.video else None
        dur = c.duration / av.time_base if c.duration else 0.0
        info = {'path': path, 'duration': dur, 'size': os.path.getsize(path), 'audio': []}
        if v is not None:
            rate = v.average_rate or v.guessed_rate
            info.update(width=v.codec_context.width, height=v.codec_context.height,
                        fps=float(rate) if rate else 30.0, codec=v.codec_context.name)
        raw = [(a.metadata.get('name') or a.metadata.get('title') or '').strip() for a in c.streams.audio]
        medal = any(n.lower() == 'all audio' for n in raw)
        info['audio_names'] = raw
        info['audio_keys'] = [n or f'#{i + 1}' for i, n in enumerate(raw)]
        info['audio_default'] = [default_track_label(i, n, medal) for i, n in enumerate(raw)]
        info['audio'] = [(names or {}).get(key) or label
                         for key, label in zip(info['audio_keys'], info['audio_default'])]
        return info


# ---------------------------------------------------------------- ses karıştırma

class TrackMix:
    """Ses kanallarının ayarı: her kanal için açık/kapalı, seviye (1.0 = %100) ve "tek dinle".
    Önizleme her 40 ms'de okur, yani oynarken değiştirmek anında duyulur."""

    def __init__(self, n):
        self.n = n
        self.on = [i == 0 for i in range(n)]   # varsayılan: ilk kanal (Medal'de "tüm ses") = orijinal
        self.gain = [1.0] * n
        self.solo = None
        self.levels = [0.0] * n                # önizlemede ölçülen son tepe (1.0 = taşma sınırı)

    def audible(self):
        """Şu an duyulan kanallar: [(sıra, seviye)]"""
        if self.solo is not None:
            return [(self.solo, self.gain[self.solo] or 1.0)]
        return [(i, self.gain[i]) for i in range(self.n) if self.on[i] and self.gain[i] > 0]

    def is_default(self):
        return self.on == [i == 0 for i in range(self.n)] and all(abs(g - 1.0) < 1e-6 for g in self.gain)

    def plan(self, mode):
        """Dışa aktarma planı: çıkıştaki her ses kanalı için [(giriş sırası, seviye)]. None = olduğu gibi kopyala."""
        active = [(i, self.gain[i]) for i in range(self.n) if self.on[i] and self.gain[i] > 0]
        if mode == 'karisim':
            return [active] if active else []
        if mode == 'ayri':
            return [[x] for x in active]
        return None


class _Track:
    """Bir ses kanalını start anından itibaren s16 stereo baytlara çevirir. Kanallar aynı andan başlasın diye
    baştaki fazlayı atar ya da eksik kalan başa sessizlik koyar."""

    def __init__(self, rate, start, end=None):
        self.res = av.AudioResampler(format='s16', layout='stereo', rate=rate)
        self.rate, self.start, self.end = rate, start, end
        self.buf = bytearray()
        self.first = True
        self.ended = False

    def feed(self, frame):
        if self.ended:
            return
        t = frame.time
        if self.end is not None and t is not None and t > self.end:
            self.ended = True
            return
        if self.first and t is not None and t + frame.samples / (frame.sample_rate or self.rate) <= self.start:
            return  # tamamen başlangıçtan önce
        data = b''.join(bytes(rf.planes[0])[:rf.samples * 4] for rf in self.res.resample(frame))
        if self.first:
            off = round(((t if t is not None else self.start) - self.start) * self.rate) * 4
            data = data[-off:] if off < 0 else bytes(off) + data
            self.first = False
        self.buf += data

    def take(self, n):
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk + bytes(n - len(chunk))


def _ready(tracks):
    """Hepsinden aynı anda alınabilecek bayt (bitmiş kanallar sessizlikle tamamlanır)."""
    live = [len(t.buf) for t in tracks if not t.ended]
    n = min(live) if live else max((len(t.buf) for t in tracks), default=0)
    return n - n % 4


def mix_bytes(parts):
    """[(s16 stereo bayt, seviye)] → tek karışım; taşan yerler kırpılır (bozulma değil, sınırda kalır)."""
    acc = None
    for data, gain in parts:
        if gain != 1.0:
            data = audioop.mul(data, 2, gain)
        acc = data if acc is None else audioop.add(acc, data, 2)
    return acc


def set_title(stream, title):
    """Çıkış kanalının adı (MP4'te Medal'in de kullandığı 'name' olarak okunur)."""
    if title:
        stream.metadata['title'] = title
        stream.metadata['handler_name'] = title


def mix_title(track, titles):
    names = [titles[k] for k, _ in track] if titles else []
    return ' + '.join(names) if 0 < len(names) <= 3 else ('Karışım' if names else None)


class MixEncoder:
    """Dışa aktarmada: plandaki her çıkış kanalı için karışımı AAC olarak kodlayıp dosyaya yazar."""

    def __init__(self, out, inp, plan, end, titles=None):
        self.out, self.plan, self.end = out, plan, end
        audio = list(inp.streams.audio)
        self.used = sorted({k for track in plan for k, _ in track})
        self.index = {audio[k].index: k for k in self.used}
        self.streams = []
        for track in plan:
            s = out.add_stream('aac', rate=MIX_RATE, layout='stereo')
            s.bit_rate = MIX_BITRATE
            set_title(s, mix_title(track, titles))
            self.streams.append((s, av.AudioResampler(format='fltp', layout='stereo', rate=MIX_RATE,
                                                      frame_size=1024)))
        self.tracks = None
        self.pending = []
        self.pts = 0
        self.limit = None

    def start(self, t0):
        """Çıkışta 0 kabul edilen an belli oldu (görüntüyle aynı): o ana kadar biriken paketler de işlenir."""
        self.tracks = {k: _Track(MIX_RATE, t0, self.end) for k in self.used}
        self.limit = max(0, round((self.end - t0) * MIX_RATE)) * 4
        pending, self.pending = self.pending, []
        for pkt in pending:
            self.feed(pkt)

    def done(self):
        return self.tracks is not None and all(t.ended for t in self.tracks.values())

    def feed(self, pkt):
        if self.tracks is None:
            self.pending.append(pkt)
            return
        track = self.tracks[self.index[pkt.stream.index]]
        for frame in pkt.decode():
            track.feed(frame)
        self._write(final=False)

    def _write(self, final):
        tracks = list(self.tracks.values())
        step = MIX_RATE // 10 * 4
        while True:
            n = min(_ready(tracks), self.limit - self.pts * 4)
            if n <= 0 or (n < step and not final):
                return
            n = min(n, step)
            chunks = {k: t.take(n) for k, t in self.tracks.items()}
            for (s, res), track in zip(self.streams, self.plan):
                frame = av.AudioFrame(format='s16', layout='stereo', samples=n // 4)
                data = mix_bytes([(chunks[k], g) for k, g in track])
                frame.planes[0].update(data + bytes(frame.planes[0].buffer_size - len(data)))
                frame.sample_rate = MIX_RATE
                frame.pts = self.pts
                frame.time_base = Fraction(1, MIX_RATE)
                for f in res.resample(frame):
                    for pk in s.encode(f):
                        self.out.mux(pk)
            self.pts += n // 4

    def finish(self):
        if self.tracks is None:
            self.start(0.0)
        for t in self.tracks.values():
            t.ended = True
        self._write(final=True)
        for s, res in self.streams:
            for f in res.resample(None):
                for pk in s.encode(f):
                    self.out.mux(pk)
            for pk in s.encode(None):
                self.out.mux(pk)


# ---------------------------------------------------------------- dışa aktarma (arka planda çalışır)

def _out_options(dst):
    return {'movflags': '+faststart'} if dst.lower().endswith(('.mp4', '.mov', '.m4v')) else {}


def _media_streams(container):
    return [s for s in container.streams if s.type in ('video', 'audio')]


def keyframe_before(src, t):
    """t anındaki ya da ondan önceki son anahtar karenin zamanı."""
    with av.open(src) as inp:
        v = inp.streams.video[0]
        inp.seek(int(t / v.time_base), stream=v, backward=True, any_frame=False)
        for pkt in inp.demux(v):
            if pkt.pts is not None and pkt.is_keyframe:
                return min(t, float(pkt.pts * v.time_base))
    return 0.0


def _audio_inputs(inp, plan):
    """Plan varsa sadece karışıma giren ses kanalları, yoksa hepsi (olduğu gibi kopya)."""
    audio = list(inp.streams.audio)
    return audio if plan is None else [audio[k] for k in sorted({k for tr in plan for k, _ in tr})]


def _copy_titles(inp, omap, titles):
    """Olduğu gibi kopyalanan ses kanallarına adlarını yaz."""
    if titles:
        for k, a in enumerate(inp.streams.audio):
            if a.index in omap and k < len(titles):
                set_title(omap[a.index], titles[k])


def cut_copy(src, dst, a, b, progress=None, cancel=None, audio_plan=None, titles=None):
    """Kayıpsız kesim: görüntüyü yeniden kodlamadan kopyalar. audio_plan verilirse ses bu plana göre
    karıştırılıp AAC olarak kodlanır, yoksa kanallar olduğu gibi kopyalanır. Döndürür: gerçek başlangıç zamanı."""
    start = keyframe_before(src, a)
    with av.open(src) as inp:
        v = inp.streams.video[0]
        streams = [v] + _audio_inputs(inp, audio_plan)
        inp.seek(int(start / v.time_base), stream=v, backward=True, any_frame=False)
        with av.open(dst, 'w', options=_out_options(dst)) as out:
            copied = streams if audio_plan is None else [v]
            omap = {s.index: out.add_stream_from_template(s) for s in copied}
            _copy_titles(inp, omap, titles)
            mixer = MixEncoder(out, inp, audio_plan, b, titles) if audio_plan else None
            zero = None           # çıkışta 0 kabul edilen an (saniye)
            done = set()
            for pkt in inp.demux(streams):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                s = pkt.stream
                if pkt.pts is None or pkt.dts is None or s.index in done:
                    continue
                if mixer is not None and s.type == 'audio':
                    mixer.feed(pkt)
                    if mixer.done():
                        done.update(x.index for x in streams if x.type == 'audio')
                    if len(done) == len(streams):
                        break
                    continue
                pts, dts = float(pkt.pts * s.time_base), float(pkt.dts * s.time_base)
                if zero is None:
                    # ilk yazılan paket, başlangıçtaki anahtar kare olmalı
                    if s.type != 'video' or not pkt.is_keyframe or pts < start - 1e-6:
                        continue
                    zero = min(pts, dts)
                    if mixer is not None:
                        mixer.start(zero)
                if s.type == 'video':
                    if dts > b:
                        done.add(s.index)
                        continue
                elif pts < zero:
                    continue
                elif pts > b:
                    done.add(s.index)
                    continue
                off = round(zero / s.time_base)
                pkt.pts -= off
                pkt.dts -= off
                pkt.stream = omap[s.index]
                out.mux(pkt)
                if progress and s.type == 'video':
                    progress(min(1.0, (pts - start) / max(0.001, b - start)))
                if len(done) == len(streams):
                    break
            if mixer is not None:
                mixer.finish()
    return start


def _pick_encoder(codec_name):
    avail = av.codecs_available
    if codec_name == 'hevc' and 'hevc_nvenc' in avail:
        return 'hevc_nvenc', {'preset': 'p5', 'rc': 'vbr', 'cq': '19', 'b': '0'}
    if 'h264_nvenc' in avail:
        return 'h264_nvenc', {'preset': 'p5', 'rc': 'vbr', 'cq': '19', 'b': '0'}
    return 'libx264', {'crf': '18', 'preset': 'veryfast'}


def cut_reencode(src, dst, a, b, progress=None, cancel=None, audio_plan=None, titles=None):
    """Tam kare kesim: görüntü yeniden kodlanır (NVIDIA varsa onunla); ses kopyalanır ya da audio_plan'a göre
    karıştırılıp AAC olarak kodlanır."""
    with av.open(src) as inp:
        v = inp.streams.video[0]
        v.thread_type = 'AUTO'
        auds = _audio_inputs(inp, audio_plan)
        enc_name, opts = _pick_encoder(v.codec_context.name)
        with av.open(dst, 'w', options=_out_options(dst)) as out:
            rate = v.average_rate or v.guessed_rate or Fraction(60)
            ov = out.add_stream(enc_name, rate=rate, options=opts)
            ov.width, ov.height = v.codec_context.width, v.codec_context.height
            ov.pix_fmt = 'yuv420p'
            ov.time_base = v.time_base
            ov.codec_context.time_base = v.time_base
            oa = {s.index: out.add_stream_from_template(s) for s in auds} if audio_plan is None else {}
            _copy_titles(inp, oa, titles)
            mixer = MixEncoder(out, inp, audio_plan, b, titles) if audio_plan else None
            if mixer is not None:
                mixer.start(a)
            inp.seek(int(a / v.time_base), stream=v, backward=True, any_frame=False)
            v_off = round(a / v.time_base)
            v_done = False
            a_done = set()
            for pkt in inp.demux(v, *auds):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                s = pkt.stream
                if s.type == 'video':
                    if v_done:
                        continue
                    for frame in pkt.decode():
                        if frame.time is None or frame.time < a - 1e-6:
                            continue
                        if frame.time > b:
                            v_done = True
                            break
                        frame.pts = frame.pts - v_off
                        frame.time_base = v.time_base
                        frame.pict_type = av.video.frame.PictureType.NONE
                        for op in ov.encode(frame.reformat(format='yuv420p')):
                            out.mux(op)
                        if progress:
                            progress(min(1.0, (frame.time - a) / max(0.001, b - a)))
                elif mixer is not None:
                    if pkt.pts is not None and s.index not in a_done:
                        mixer.feed(pkt)
                        if mixer.done():
                            a_done.update(x.index for x in auds)
                else:
                    if pkt.pts is None or s.index in a_done:
                        continue
                    t = float(pkt.pts * s.time_base)
                    if t < a:
                        continue
                    if t > b:
                        a_done.add(s.index)
                        continue
                    off = round(a / s.time_base)
                    pkt.pts -= off
                    pkt.dts -= off
                    pkt.stream = oa[s.index]
                    out.mux(pkt)
                if v_done and len(a_done) == len(auds):
                    break
            for op in ov.encode(None):
                out.mux(op)
            if mixer is not None:
                mixer.finish()
    return a


def stream_signature(path):
    """Birleştirme uyumluluğu için akışların özeti."""
    sig = []
    with av.open(path) as c:
        for s in _media_streams(c):
            cc = s.codec_context
            if s.type == 'video':
                sig.append(('video', cc.name, cc.width, cc.height, bytes(cc.extradata or b'')))
            else:
                sig.append(('audio', cc.name, cc.sample_rate, cc.layout.name if cc.layout else '',
                            bytes(cc.extradata or b'')))
    return sig


def check_concat(files):
    """Hepsi aynı ayarlarla mı kaydedilmiş? Sorun varsa açıklama döndürür, yoksa None."""
    base = stream_signature(files[0])
    for f in files[1:]:
        sig = stream_signature(f)
        if len(sig) != len(base):
            return f'{os.path.basename(f)}: akış sayısı farklı ({len(sig)} ≠ {len(base)})'
        for x, y in zip(base, sig):
            if x[:4] != y[:4]:
                return f'{os.path.basename(f)}: farklı ayarlarla kaydedilmiş ({x[0]}: {x[1:4]} ≠ {y[1:4]})'
            if x[4] != y[4]:
                return f'{os.path.basename(f)}: kodlayıcı ayarları farklı (kayıpsız birleştirilemez)'
    return None


def concat_copy(files, dst, progress=None, cancel=None):
    """Klipleri kayıpsız uç uca ekler (hepsi aynı ayarlarla kaydedilmiş olmalı)."""
    total = sum(os.path.getsize(f) for f in files)
    done_b = 0
    with av.open(files[0]) as first, av.open(dst, 'w', options=_out_options(dst)) as out:
        omap = [out.add_stream_from_template(s) for s in _media_streams(first)]
        offset = 0.0
        for path in files:
            with av.open(path) as inp:
                streams = _media_streams(inp)
                starts = [float(s.start_time * s.time_base) for s in streams if s.start_time is not None]
                zero = min(starts) if starts else 0.0
                end = offset
                for pkt in inp.demux(streams):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if pkt.pts is None or pkt.dts is None:
                        continue
                    s = pkt.stream
                    off = round((offset - zero) / s.time_base)
                    pkt.pts += off
                    pkt.dts += off
                    end = max(end, float((pkt.pts + (pkt.duration or 0)) * s.time_base))
                    done_b += pkt.size
                    pkt.stream = omap[streams.index(s)]
                    out.mux(pkt)
                    if progress:
                        progress(min(1.0, done_b / max(1, total)))
                offset = end
    return offset


# ---------------------------------------------------------------- önizleme oynatıcı

class AudioOut(threading.Thread):
    """Ses kanallarını TrackMix'teki ayarlarla karıştırıp start'tan itibaren varsayılan ses çıkışına çalar.
    Ayarlar çalarken değişebilir; her kanalın seviyesi göstergeler için mix.levels'a yazılır."""

    CHUNK = 0.04  # sn: ayar değişikliği en geç bu kadar sonra duyulur

    def __init__(self, path, mix, start, end, stop):
        super().__init__(daemon=True)
        self.path, self.mix, self.start_t, self.end_t, self.stop = path, mix, start, end, stop

    def run(self):
        try:
            rate = int(sd.query_devices(kind='output')['default_samplerate'])
            with av.open(self.path) as c:
                streams = list(c.streams.audio)
                if not streams:
                    return
                c.seek(int(self.start_t / streams[0].time_base), stream=streams[0])
                tracks = [_Track(rate, self.start_t, self.end_t) for _ in streams]
                order = {s.index: k for k, s in enumerate(streams)}
                chunk = int(rate * self.CHUNK) * 4
                with sd.RawOutputStream(samplerate=rate, channels=2, dtype='int16') as out:
                    for pkt in c.demux(streams):
                        if self.stop.is_set():
                            return
                        track = tracks[order[pkt.stream.index]]
                        if not track.ended:
                            for frame in pkt.decode():
                                track.feed(frame)
                        while _ready(tracks) >= chunk:
                            if self.stop.is_set():
                                return
                            out.write(self._mix(tracks, chunk))
                        if all(t.ended for t in tracks):
                            break
                    for t in tracks:
                        t.ended = True
                    n = _ready(tracks)
                    if n and not self.stop.is_set():
                        out.write(self._mix(tracks, n))
        except Exception as e:  # ses yoksa önizleme sessiz devam eder
            print('ses hatası:', e)
        finally:
            self.mix.levels = [0.0] * self.mix.n

    def _mix(self, tracks, n):
        mix = self.mix
        chunks = [t.take(n) for t in tracks]
        mix.levels = [audioop.max(ch, 2) / 32768 * mix.gain[k] for k, ch in enumerate(chunks)]
        parts = [(chunks[k], g) for k, g in mix.audible() if k < len(chunks)]
        return mix_bytes(parts) if parts else bytes(n)


class Player:
    """Kareleri ayrı iş parçacığında çözer; en son kare self.latest'te durur (arayüz alır)."""

    def __init__(self, path, info, box=(PREVIEW_W, PREVIEW_H)):
        self.path, self.info = path, info
        self.box = box
        self.lock = threading.Lock()
        self.cmd = None
        self.event = threading.Event()
        self.latest = None          # (PIL görüntü, zaman)
        self.pos = 0.0
        self.playing = False
        self.mix = None             # TrackMix (düzenleyici verir)
        self.muted = False
        self.display_fps = DISPLAY_FPS
        self.emitted = 0            # ekrana gönderilen kare sayısı (FPS göstergesi için)
        self.size = self._fit(info.get('width') or 16, info.get('height') or 9)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _fit(self, w, h):
        scale = min(self.box[0] / w, self.box[1] / h)
        return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)

    def request(self, *cmd):
        with self.lock:
            self.cmd = cmd
        self.event.set()

    def _pending(self):
        with self.lock:
            return self.cmd is not None

    def _emit(self, frame):
        img = frame.reformat(width=self.size[0], height=self.size[1], format='rgb24').to_image()
        self.pos = frame.time or 0.0
        with self.lock:
            self.latest = (img, self.pos)
            self.emitted += 1

    def _run(self):
        c = av.open(self.path)
        v = c.streams.video[0]
        v.thread_type = 'AUTO'
        try:
            while True:
                self.event.wait()
                with self.lock:
                    cmd, self.cmd = self.cmd, None
                    self.event.clear()
                if not cmd:
                    continue
                if cmd[0] == 'close':
                    return
                if cmd[0] == 'seek':
                    self._show_at(c, v, cmd[1])
                elif cmd[0] == 'play':
                    self._play(c, v, cmd[1], cmd[2])
        finally:
            c.close()

    def _show_at(self, c, v, t):
        t = max(0.0, min(t, max(0.0, self.info['duration'] - 0.001)))
        c.seek(int(t / v.time_base), stream=v, backward=True, any_frame=False)
        last = None
        for frame in c.decode(v):
            if frame.time is not None and frame.time >= t - 0.5 / max(1.0, self.info['fps']):
                self._emit(frame)
                return
            last = frame
            if self._pending():  # yeni istek geldi (hızlı sürükleme): bu kareyi bırak
                return
        if last is not None:
            self._emit(last)

    def _play(self, c, v, start, end):
        self.playing = True
        stop = threading.Event()
        if sd is not None and not self.muted and self.mix is not None:
            AudioOut(self.path, self.mix, start, end, stop).start()
        try:
            c.seek(int(start / v.time_base), stream=v, backward=True, any_frame=False)
            t0 = time.perf_counter() + 0.12   # ses çıkışının gecikmesine kabaca eşitle
            shown = -1.0
            gap = 1.0 / self.display_fps - 0.002 if self.display_fps else 0.0
            for frame in c.decode(v):
                if self._pending():
                    return
                ft = frame.time
                if ft is None or ft < start - 1e-6:
                    continue
                if end is not None and ft > end:
                    return
                now = start + (time.perf_counter() - t0)
                if ft < now - 0.05 or ft - shown < gap:
                    continue  # geride kaldıysak ya da ekrana gerek yoksa atla (çözmeye devam)
                wait = ft - (start + (time.perf_counter() - t0))
                if wait > 0 and self.event.wait(wait):
                    return
                self._emit(frame)
                shown = ft
        finally:
            stop.set()
            self.playing = False

    def close(self):
        self.request('close')


# ---------------------------------------------------------------- zaman çubuğu

class Timeline(tk.Canvas):
    def __init__(self, parent, on_seek, bg):
        super().__init__(parent, height=46, highlightthickness=0, bg=bg, cursor='hand2')
        self.on_seek = on_seek
        self.duration = 0.0
        self.pos = 0.0
        self.a = None
        self.b = None
        self.bind('<Configure>', lambda _e: self.redraw())
        self.bind('<Button-1>', self._click)
        self.bind('<B1-Motion>', self._click)

    def _x(self, t):
        w = max(1, self.winfo_width() - 16)
        return 8 + w * (t / self.duration if self.duration else 0)

    def _click(self, e):
        if not self.duration:
            return
        w = max(1, self.winfo_width() - 16)
        self.on_seek(max(0.0, min(self.duration, (e.x - 8) / w * self.duration)))

    def set(self, duration=None, pos=None, a=..., b=...):
        if duration is not None:
            self.duration = duration
        if pos is not None:
            self.pos = pos
        if a is not ...:
            self.a = a
        if b is not ...:
            self.b = b
        self.redraw()

    def redraw(self):
        self.delete('all')
        w = self.winfo_width()
        y0, y1 = 14, 30
        self.create_rectangle(8, y0, w - 8, y1, fill='#2b2b2b', outline='#3a3a3a')
        if not self.duration:
            return
        a = self.a if self.a is not None else 0.0
        b = self.b if self.b is not None else self.duration
        if self.a is not None or self.b is not None:
            self.create_rectangle(self._x(a), y0, self._x(b), y1, fill='#1e3a8a', outline='')
        step = max(1, int(self.duration / 12))
        for s in range(0, int(self.duration) + 1, step):
            x = self._x(s)
            self.create_line(x, y1, x, y1 + 4, fill='#555')
            self.create_text(min(max(x, 22), w - 22), y1 + 11, text=fmt_time(s)[:-3], fill='#8a8a8a',
                             font=('Segoe UI', 8))
        for t, color, label in ((self.a, GREEN, 'A'), (self.b, RED, 'B')):
            if t is not None:
                x = self._x(t)
                self.create_line(x, y0 - 6, x, y1, fill=color, width=2)
                self.create_text(x, y0 - 8, text=label, fill=color, font=('Segoe UI Semibold', 9))
        x = self._x(self.pos)
        self.create_line(x, y0 - 3, x, y1 + 3, fill='white', width=2)
        self.create_polygon(x - 5, y0 - 8, x + 5, y0 - 8, x, y0 - 2, fill='white')


# ---------------------------------------------------------------- sekme

class EditorTab:
    def __init__(self, parent, app, names_get=None, names_set=None):
        self.app = app
        self.parent = parent
        self.names_get = names_get or (lambda: {})           # kanal adları: {orijinal: verilen}
        self.names_set = names_set or (lambda key, value: None)
        self.player = None
        self.info = None
        self.photo = None
        self.shown_t = None
        self._shown = []          # son 1 sn'de ekrana basılan karelerin zamanları (FPS göstergesi)
        self._fps_shown_at = 0.0
        self.job_cancel = None
        self.mix = None
        self.mix_rows = []
        self._meter_at = 0.0
        self._auto_mode = False
        self.bg = ttk.Style(parent).lookup('TFrame', 'background') or '#1c1c1c'

        nb = ttk.Notebook(parent)
        nb.pack(fill='both', expand=True)
        cut = ttk.Frame(nb, padding=10)
        merge = ttk.Frame(nb, padding=10)
        nb.add(cut, text='  Önizle / Kes  ')
        nb.add(merge, text='  Birleştir  ')
        self._build_cut(cut)
        self._build_merge(merge)
        self._loop_job = None
        self._frame_loop()

    def close(self):
        """Pencerede ekran değişirken (ör. Telegram çıkışı): ekran döngüsünü ve oynatıcıyı kapatır."""
        if self._loop_job:
            self.parent.after_cancel(self._loop_job)
            self._loop_job = None
        if self.player:
            self.player.close()
            self.player = None

    # ------------------------------------------------ Kes
    def _build_cut(self, t):
        row = ttk.Frame(t)
        row.pack(fill='x')
        ttk.Button(row, text='Klip aç…', style='Accent.TButton', command=self.open_file).pack(side='left')
        ttk.Button(row, text='Oynatıcıda aç', command=self.open_external).pack(side='left', padx=8)
        self.file_lbl = ttk.Label(row, text='Bir klip aç (Medal klasöründen ya da Geri Yükle ile indirdiğin yerden).',
                                  style='Muted.TLabel')
        self.file_lbl.pack(side='left', padx=8)

        # Önizleme alanı: ekran ölçeklemesine ve yüksekliğine göre (küçük ekranda alttaki tuşlar kaybolmasın)
        scale = max(1.0, t.winfo_fpixels('1i') / 96.0)
        ph = int(min(PREVIEW_H * scale, max(240, t.winfo_screenheight() - 90 - 590 * scale)))
        self.box = (ph * 16 // 9, ph)
        box = tk.Frame(t, bg='black', width=self.box[0], height=self.box[1])
        box.pack(pady=(10, 6))
        box.pack_propagate(False)
        self.screen = tk.Label(box, bg='black', fg=GREY, text='Önizleme', font=('Segoe UI', 12))
        self.screen.pack(fill='both', expand=True)

        self.timeline = Timeline(t, self.seek, self.bg)
        self.timeline.pack(fill='x')

        ctl = ttk.Frame(t)
        ctl.pack(fill='x', pady=(6, 0))
        for text, cmd in (('⏮ 5s', lambda: self.jump(-5)), ('◀ kare', lambda: self.step(-1))):
            ttk.Button(ctl, text=text, command=cmd, width=6).pack(side='left', padx=(0, 3))
        self.play_btn = ttk.Button(ctl, text='▶ Oynat', command=self.toggle_play, width=9)
        self.play_btn.pack(side='left', padx=(0, 3))
        for text, cmd in (('kare ▶', lambda: self.step(1)), ('5s ⏭', lambda: self.jump(5))):
            ttk.Button(ctl, text=text, command=cmd, width=6).pack(side='left', padx=(0, 3))
        self.time_lbl = ttk.Label(ctl, text='00:00,00 / 00:00,00', font=('Consolas', 11))
        self.time_lbl.pack(side='left', padx=8)
        self.fps_lbl = ttk.Label(ctl, text='', style='Muted.TLabel', width=7)
        self.fps_lbl.pack(side='left')
        self.mute = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text='Sessiz', variable=self.mute, command=self._mute_changed).pack(side='right')
        self.fps_cb = ttk.Combobox(ctl, values=['30 FPS', '60 FPS', 'Tam FPS'], width=8, state='readonly')
        self.fps_cb.current(1)
        self.fps_cb.pack(side='right', padx=(6, 0))
        self.fps_cb.bind('<<ComboboxSelected>>', lambda _e: self._fps_changed())

        # ses kanalları (klip açılınca dolar)
        self.mix_box = ttk.Frame(t)
        self.mix_box.pack(fill='x', pady=(8, 0))

        sel = ttk.Frame(t)
        sel.pack(fill='x', pady=(8, 0))
        ttk.Button(sel, text='[ A  Başlangıç', command=self.set_a).pack(side='left')
        ttk.Button(sel, text='B  Bitiş ]', command=self.set_b).pack(side='left', padx=6)
        ttk.Button(sel, text='▶ Seçimi oynat', command=self.play_selection).pack(side='left')
        ttk.Button(sel, text='Seçimi temizle', command=self.clear_sel).pack(side='left', padx=6)
        self.sel_lbl = ttk.Label(sel, text='Seçim yok: A ve B ile işaretle', style='Muted.TLabel')
        self.sel_lbl.pack(side='left', padx=10)

        r = ttk.Frame(t)
        r.pack(fill='x', pady=(10, 0))
        ttk.Label(r, text='Kesim:').pack(side='left')
        self.mode = ttk.Combobox(r, values=['Kayıpsız (hızlı)', 'Tam kare (NVIDIA)'], width=18, state='readonly')
        self.mode.current(0)
        self.mode.pack(side='left', padx=6)
        self.mode.bind('<<ComboboxSelected>>', lambda _e: self._mode_hint())
        ttk.Label(r, text='  Ses:').pack(side='left')
        self.audio_mode = ttk.Combobox(r, values=AUDIO_MODES, width=20, state='readonly')
        self.audio_mode.current(0)
        self.audio_mode.pack(side='left', padx=6)
        self.audio_mode.bind('<<ComboboxSelected>>', lambda _e: self._mode_hint())
        self.cut_btn = ttk.Button(r, text='Kes ve kaydet…', style='Accent.TButton', command=self.export_cut)
        self.cut_btn.pack(side='left', padx=(6, 0))
        self.cut_stop = ttk.Button(r, text='İptal', command=self.cancel_job, state='disabled')
        self.cut_stop.pack(side='left', padx=6)
        self.cut_bar = ttk.Progressbar(r, maximum=100, length=200)
        self.cut_bar.pack(side='left', padx=8)
        self.cut_msg = ttk.Label(r, text='', style='Muted.TLabel')
        self.cut_msg.pack(side='left', padx=6)
        self.mode_hint = ttk.Label(t, text='', style='Muted.TLabel', wraplength=940, justify='left')
        self.mode_hint.pack(anchor='w', pady=(4, 0))
        self._mode_hint()
        for key, fn in (('<space>', lambda e: self.toggle_play()), ('<Left>', lambda e: self.jump(-1)),
                        ('<Right>', lambda e: self.jump(1)), ('<comma>', lambda e: self.step(-1)),
                        ('<period>', lambda e: self.step(1)), ('a', lambda e: self.set_a()),
                        ('b', lambda e: self.set_b())):
            self.timeline.bind(key, fn)
            self.screen.bind(key, fn)
        for w in (self.timeline, self.screen):
            w.bind('<Button-1>', lambda e, w=w: w.focus_set(), add='+')

    def _mode_hint(self):
        if self.mode.current() == 1:
            text = ('Tam kare: tam A karesinden başlar. Görüntü NVIDIA ekran kartıyla yüksek kalitede yeniden '
                    'kodlanır. Klip uzunluğu kadar sürebilir.')
        else:
            text = ('Kayıpsız: görüntüyü yeniden kodlamadan kopyalar, saniyeler sürer ve kalite birebir aynı kalır. '
                    'Başlangıç en yakın önceki anahtar kareye (genelde <0,5 sn önce) denk gelir.')
        m = self.audio_mode.current()
        if m == 1:
            text += ('  Ses: açık kanallar ayarladığın seviyelerle tek kanalda birleşir (AAC); Discord\'da ve '
                     'telefonda hepsi duyulur.')
        elif m == 2:
            text += '  Ses: açık kanallar ayrı ayrı, ayarladığın seviyelerle kaydedilir (AAC).'
        else:
            text += ('  Ses: bütün kanallar olduğu gibi kopyalanır (Discord\'da ve telefonda sadece ilk kanal '
                     'çalar).')
        self.mode_hint.configure(text=text)

    def open_file(self, path=None):
        if not path:
            path = filedialog.askopenfilename(title='Klip aç', filetypes=VIDEO_TYPES)
        if not path:
            return
        try:
            info = probe(path, self.names_get())
        except Exception as e:
            messagebox.showerror('Klip Kalkanı', f'Açılamadı:\n{e}')
            return
        if 'width' not in info:
            messagebox.showerror('Klip Kalkanı', 'Bu dosyada görüntü yok.')
            return
        if self.player:
            self.player.close()
        self.info = info
        self.player = Player(path, info, self.box)
        self.player.muted = self.mute.get()
        self.player.mix = self.mix
        i = self.fps_cb.current()
        self.fps_cb.configure(values=['30 FPS', '60 FPS', f'Tam ({info["fps"]:.0f})'])
        self.fps_cb.current(i)
        self._fps_changed()
        self.a = self.b = None
        self._build_mixer()
        self.file_lbl.configure(text=f'{os.path.basename(path)}  ·  {info["width"]}×{info["height"]}  ·  '
                                     f'{info["fps"]:.0f} FPS  ·  {info["codec"].upper()}  ·  '
                                     f'{fmt_time(info["duration"])}  ·  {fmt_size(info["size"])}  ·  '
                                     f'{len(info["audio"])} ses kanalı', style='TLabel')
        self.timeline.set(duration=info['duration'], pos=0.0, a=None, b=None)
        self._update_sel()
        self.player.request('seek', 0.0)
        self.timeline.focus_set()

    def open_external(self):
        if self.info:
            os.startfile(self.info['path'])

    # oynatma
    def seek(self, t):
        if self.player:
            self.player.request('seek', t)
            self.timeline.set(pos=t)
            self.play_btn.configure(text='▶ Oynat')

    def jump(self, d):
        if self.player:
            self.seek(max(0.0, min(self.info['duration'], self.player.pos + d)))

    def step(self, d):
        if self.player:
            self.seek(max(0.0, self.player.pos + d / max(1.0, self.info['fps'])))

    def toggle_play(self):
        if not self.player:
            return
        if self.player.playing:
            self.player.request('seek', self.player.pos)
            self.play_btn.configure(text='▶ Oynat')
        else:
            start = self.player.pos if self.player.pos < self.info['duration'] - 0.1 else 0.0
            self.player.request('play', start, None)
            self.play_btn.configure(text='⏸ Durdur')

    def play_selection(self):
        if self.player:
            a = self.a if self.a is not None else 0.0
            b = self.b if self.b is not None else self.info['duration']
            self.player.request('play', a, b)
            self.play_btn.configure(text='⏸ Durdur')

    def _mute_changed(self):
        if self.player:
            self.player.muted = self.mute.get()

    def _fps_changed(self):
        if not self.player:
            return
        i = self.fps_cb.current()
        self.player.display_fps = {0: 30, 1: 60}.get(i, 0)
        if self.player.playing:  # yeni ayarla kaldığı yerden sürsün
            self.player.request('play', self.player.pos, None)

    # ------------------------------------------------ ses kanalları
    def _build_mixer(self):
        for w in self.mix_box.winfo_children():
            w.destroy()
        n = len(self.info['audio'])
        self.mix = TrackMix(n) if n else None
        self.mix_rows = []
        self._auto_mode = False
        self.audio_mode.current(0)
        self.audio_mode.configure(state='readonly' if n else 'disabled')
        self._mode_hint()
        if self.player is not None:
            self.player.mix = self.mix
        if not n:
            ttk.Label(self.mix_box, text='Bu klipte ses yok.', style='Muted.TLabel').pack(anchor='w')
            return
        head = ttk.Frame(self.mix_box)
        head.pack(fill='x')
        ttk.Label(head, text='Ses kanalları', font=('Segoe UI Semibold', 10)).pack(side='left')
        ttk.Label(head, text='oynatırken değiştir, anında duyarsın · sürgüye çift tıkla: %100 · ✎ adını değiştir',
                  style='Muted.TLabel').pack(side='left', padx=10)
        if n > 1:
            ttk.Button(head, text='Sıfırla', command=self.mix_reset).pack(side='right')
            if (self.info.get('audio_names') or [''])[0].lower() == 'all audio':
                ttk.Button(head, text='Kanalları ayrı ayarla', command=self.mix_split).pack(side='right', padx=6)
        grid = ttk.Frame(self.mix_box)
        grid.pack(fill='x', pady=(4, 0))
        for k, label in enumerate(self.info['audio']):
            cell = ttk.Frame(grid, padding=(0, 0, 16, 0))
            cell.grid(row=k // 4, column=k % 4, sticky='we')
            grid.columnconfigure(k % 4, weight=1, uniform='kanal')
            top = ttk.Frame(cell)
            top.pack(fill='x')
            on = tk.BooleanVar(value=self.mix.on[k])
            chk = ttk.Checkbutton(top, text=label, variable=on, command=lambda k=k: self._mix_changed(k))
            chk.pack(side='left')
            ren = ttk.Button(top, text='✎', width=2, command=lambda k=k: self.rename_track(k))
            ren.pack(side='left', padx=(2, 0))
            solo = ttk.Button(top, text='Tek dinle', width=9, command=lambda k=k: self.mix_solo(k))
            solo.pack(side='right')
            pct = ttk.Label(top, text='%100', width=5, anchor='e')
            pct.pack(side='right', padx=4)
            val = tk.DoubleVar(value=100.0)
            scale = ttk.Scale(cell, from_=0, to=MAX_GAIN, variable=val, command=lambda _v, k=k: self._mix_changed(k))
            scale.pack(fill='x')
            scale.bind('<Double-Button-1>', lambda _e, k=k: self._mix_set(k, on=None, pct=100))
            meter = tk.Canvas(cell, height=6, highlightthickness=0, bg='#2b2b2b')
            meter.pack(fill='x', pady=(2, 0))
            self.mix_rows.append({'on': on, 'val': val, 'pct': pct, 'solo': solo, 'meter': meter, 'shown': 0.0,
                                  'top': top, 'chk': chk, 'ren': ren, 'entry': None})

    def rename_track(self, k):
        """Kanal adını yerinde düzenle: Enter kaydeder, Esc vazgeçer, boş bırakılırsa varsayılan ada döner."""
        row = self.mix_rows[k]
        if row['entry'] is not None:
            return
        entry = ttk.Entry(row['top'], width=18)
        entry.insert(0, self.info['audio'][k])
        entry.select_range(0, 'end')
        row['chk'].pack_forget()
        entry.pack(side='left', before=row['ren'])
        entry.focus_set()
        row['entry'] = entry

        def done(save):
            if row['entry'] is not entry:
                return
            text = entry.get().strip()
            row['entry'] = None
            entry.destroy()
            row['chk'].pack(side='left', before=row['ren'])
            if save:
                self.set_track_name(k, text)
        entry.bind('<Return>', lambda _e: done(True))
        entry.bind('<KP_Enter>', lambda _e: done(True))
        entry.bind('<Escape>', lambda _e: done(False))
        entry.bind('<FocusOut>', lambda _e: done(True))

    def set_track_name(self, k, text):
        default = self.info['audio_default'][k]
        custom = text if text and text != default else None
        self.names_set(self.info['audio_keys'][k], custom)
        self.info['audio'][k] = custom or default
        self.mix_rows[k]['chk'].configure(text=self.info['audio'][k])

    def _mix_changed(self, k):
        row = self.mix_rows[k]
        value = round(row['val'].get())
        self.mix.on[k] = bool(row['on'].get())
        self.mix.gain[k] = value / 100
        row['pct'].configure(text=f'%{value}')
        # ayar değişince kayıtta da uygulansın: kendiliğinden "tek kanal" seçilir (elle değiştirilebilir);
        # ayarlar varsayılana dönerse yine "olduğu gibi"
        if not self._auto_mode and not self.mix.is_default() and self.audio_mode.current() == 0:
            self._auto_mode = True
            self.audio_mode.current(1)
            self._mode_hint()
        elif self._auto_mode and self.mix.is_default() and self.audio_mode.current() == 1:
            self._auto_mode = False
            self.audio_mode.current(0)
            self._mode_hint()

    def _mix_set(self, k, on=None, pct=None):
        row = self.mix_rows[k]
        if on is not None:
            row['on'].set(on)
        if pct is not None:
            row['val'].set(pct)
        self._mix_changed(k)

    def mix_split(self):
        """Medal: "tüm ses"i kapat, oyun / Discord / mikrofonu ayrı ayrı aç."""
        for k in range(len(self.mix_rows)):
            self._mix_set(k, on=k > 0, pct=100)

    def mix_reset(self):
        for k in range(len(self.mix_rows)):
            self._mix_set(k, on=k == 0, pct=100)
        if self.mix.solo is not None:
            self.mix_solo(self.mix.solo)

    def mix_solo(self, k):
        """Sadece bu kanalı dinle (tekrar basınca normale döner)."""
        self.mix.solo = None if self.mix.solo == k else k
        for i, row in enumerate(self.mix_rows):
            row['solo'].configure(text='● Dinleniyor' if self.mix.solo == i else 'Tek dinle',
                                  style='Accent.TButton' if self.mix.solo == i else 'TButton')

    def _draw_meters(self, playing):
        mix = self.mix
        audible = {k for k, _ in mix.audible()}
        for k, row in enumerate(self.mix_rows):
            level = mix.levels[k] if playing and k < len(mix.levels) else 0.0
            shown = max(level, row['shown'] * 0.8)   # yavaş düşsün
            row['shown'] = shown
            c = row['meter']
            w = max(1, c.winfo_width())
            db = 20 * math.log10(shown) if shown > 1e-4 else -80.0
            frac = max(0.0, min(1.0, (db + 60) / 60))
            color = GREY if k not in audible else (RED if db > -1 else YELLOW if db > -9 else GREEN)
            c.delete('all')
            if frac > 0:
                c.create_rectangle(0, 0, int(w * frac), 6, fill=color, outline='')

    def _frame_loop(self):
        p = self.player
        if p is not None:
            with p.lock:
                latest = p.latest
            if latest is not None and latest[1] != self.shown_t:
                img, t = latest
                if self.photo is not None and (self.photo.width(), self.photo.height()) == img.size:
                    self.photo.paste(img)   # aynı resmin üstüne yaz: hızlı
                else:
                    self.photo = ImageTk.PhotoImage(img)
                    self.screen.configure(image=self.photo, text='')
                self.shown_t = t
                self.timeline.set(pos=t)
                self.time_lbl.configure(text=f'{fmt_time(t)} / {fmt_time(self.info["duration"])}')
                self._shown.append(time.perf_counter())
            now = time.perf_counter()
            while self._shown and self._shown[0] < now - 1.0:
                self._shown.pop(0)
            if now - self._fps_shown_at > 0.5:
                self.fps_lbl.configure(text=f'{len(self._shown)} fps' if p.playing else '')
                self._fps_shown_at = now
            if not p.playing and self.play_btn.cget('text') != '▶ Oynat' and not p._pending():
                self.play_btn.configure(text='▶ Oynat')
            if self.mix is not None and now - self._meter_at > 0.05:
                self._meter_at = now
                self._draw_meters(p.playing)
        self._loop_job = self.parent.after(4, self._frame_loop)

    # seçim
    def set_a(self):
        if self.player:
            self.a = self.player.pos
            if self.b is not None and self.b <= self.a:
                self.b = None
            self._update_sel()

    def set_b(self):
        if self.player:
            self.b = self.player.pos
            if self.a is not None and self.a >= self.b:
                self.a = None
            self._update_sel()

    def clear_sel(self):
        self.a = self.b = None
        self._update_sel()

    def _update_sel(self):
        self.timeline.set(a=self.a, b=self.b)
        if self.a is None and self.b is None:
            self.sel_lbl.configure(text='Seçim yok: A ve B ile işaretle (kısayol: a, b, boşluk, ←, →, virgül, nokta)')
            return
        a = self.a if self.a is not None else 0.0
        b = self.b if self.b is not None else self.info['duration']
        self.sel_lbl.configure(text=f'Seçim: {fmt_time(a)} → {fmt_time(b)}  ({fmt_time(b - a)})')

    # dışa aktarma
    def _run_job(self, fn, done, bar, msg, buttons):
        cancel = threading.Event()
        self.job_cancel = cancel
        for b in buttons:
            b.configure(state='disabled')
        self.cut_stop.configure(state='normal')
        self.merge_stop.configure(state='normal')
        state = {'p': 0.0}

        def progress(p):
            state['p'] = p

        def tick():
            bar['value'] = 100 * state['p']
            if not state.get('end'):
                msg.configure(text=f'%{100 * state["p"]:.0f}'.replace('.', ','))
                self.parent.after(200, tick)

        def work():
            try:
                res = (True, fn(progress, cancel))
            except Exception as e:
                res = (False, e)
            state['end'] = True
            self.app.post(finish, res)

        def finish(res):
            for b in buttons:
                b.configure(state='normal')
            self.cut_stop.configure(state='disabled')
            self.merge_stop.configure(state='disabled')
            self.job_cancel = None
            ok, val = res
            if ok:
                bar['value'] = 100
            done(ok, val)

        threading.Thread(target=work, daemon=True).start()
        tick()

    def cancel_job(self):
        if self.job_cancel:
            self.job_cancel.set()

    def export_cut(self):
        if not self.info:
            return
        a = self.a if self.a is not None else 0.0
        b = self.b if self.b is not None else self.info['duration']
        if b - a < 0.2:
            messagebox.showwarning('Klip Kalkanı', 'Seçim çok kısa.')
            return
        src = self.info['path']
        root, ext = os.path.splitext(src)
        dst = filedialog.asksaveasfilename(title='Kesilen klibi kaydet', initialdir=os.path.dirname(src),
                                           initialfile=f'{os.path.basename(root)}_kesit{ext}',
                                           defaultextension=ext, filetypes=VIDEO_TYPES)
        if not dst:
            return
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
            messagebox.showerror('Klip Kalkanı', 'Orijinal dosyanın üstüne yazılamaz, başka bir ad seç.')
            return
        exact = self.mode.current() == 1
        plan = self.mix.plan((None, 'karisim', 'ayri')[self.audio_mode.current()]) if self.mix else None
        if plan == [] and not messagebox.askyesno('Klip Kalkanı', 'Hiç ses kanalı açık değil. Klip sessiz '
                                                                  'kaydedilsin mi?'):
            return
        cut = cut_reencode if exact else cut_copy
        titles = list(self.info['audio'])
        fn = lambda p, c: cut(src, dst, a, b, p, c, audio_plan=plan, titles=titles)  # noqa: E731
        self.cut_msg.configure(text='Kesiliyor…')

        def done(ok, val):
            if ok:
                note = '' if exact or abs(val - a) < 0.01 else f' (anahtar kare yüzünden {fmt_time(val)}\'dan başladı)'
                self.cut_msg.configure(text=f'✔ Kaydedildi: {os.path.basename(dst)}{note}')
            else:
                self._cleanup(dst)
                self.cut_msg.configure(text='İptal edildi.' if isinstance(val, Cancelled) else f'Hata: {val}')
        self._run_job(fn, done, self.cut_bar, self.cut_msg, [self.cut_btn, self.merge_btn])

    @staticmethod
    def _cleanup(path):
        # yarım kalan çıktı (bizim az önce oluşturduğumuz dosya) silinir; orijinallere dokunulmaz
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------ Birleştir
    def _build_merge(self, t):
        ttk.Label(t, text='Klipleri uç uca ekle', style='H2.TLabel').pack(anchor='w')
        ttk.Label(t, text='Aynı programla ve aynı ayarlarla kaydedilmiş klipler (ör. iki Medal klibi) kayıpsız ve '
                          'saniyeler içinde birleşir. Sıra yukarıdan aşağıya.', style='Muted.TLabel',
                  wraplength=900).pack(anchor='w', pady=(2, 10))
        body = ttk.Frame(t)
        body.pack(fill='both', expand=True)
        lst = tk.Listbox(body, height=14, activestyle='none', selectmode='browse', bg='#232323', fg='white',
                         highlightthickness=0, selectbackground='#1e3a8a', font=('Segoe UI', 10))
        lst.pack(side='left', fill='both', expand=True)
        self.merge_list = lst
        self.merge_files = []
        side = ttk.Frame(body)
        side.pack(side='left', fill='y', padx=(10, 0))
        for text, cmd in (('Ekle…', self.merge_add), ('Önizlemedeki klibi ekle', self.merge_add_current),
                          ('▲ Yukarı', lambda: self.merge_move(-1)), ('▼ Aşağı', lambda: self.merge_move(1)),
                          ('Listeden çıkar', self.merge_remove)):
            ttk.Button(side, text=text, command=cmd).pack(fill='x', pady=3)
        r = ttk.Frame(t)
        r.pack(fill='x', pady=(10, 0))
        self.merge_btn = ttk.Button(r, text='Birleştir ve kaydet…', style='Accent.TButton', command=self.export_merge)
        self.merge_btn.pack(side='left')
        self.merge_stop = ttk.Button(r, text='İptal', command=self.cancel_job, state='disabled')
        self.merge_stop.pack(side='left', padx=6)
        self.merge_bar = ttk.Progressbar(r, maximum=100, length=260)
        self.merge_bar.pack(side='left', padx=8)
        self.merge_msg = ttk.Label(r, text='', style='Muted.TLabel')
        self.merge_msg.pack(side='left', padx=6)

    def _merge_refresh(self):
        self.merge_list.delete(0, 'end')
        total = 0.0
        for i, (p, info) in enumerate(self.merge_files, 1):
            total += info['duration']
            self.merge_list.insert('end', f'{i}.  {os.path.basename(p)}   ({fmt_time(info["duration"])}, '
                                          f'{info["width"]}×{info["height"]}, {fmt_size(info["size"])})')
        self.merge_msg.configure(text=f'{len(self.merge_files)} klip, toplam {fmt_time(total)}' if self.merge_files
                                 else '')

    def _merge_add_path(self, p):
        try:
            self.merge_files.append((p, probe(p)))
        except Exception as e:
            messagebox.showerror('Klip Kalkanı', f'{os.path.basename(p)} açılamadı:\n{e}')
        self._merge_refresh()

    def merge_add(self):
        for p in filedialog.askopenfilenames(title='Birleştirilecek klipler', filetypes=VIDEO_TYPES):
            self._merge_add_path(p)

    def merge_add_current(self):
        if self.info:
            self._merge_add_path(self.info['path'])

    def merge_move(self, d):
        sel = self.merge_list.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + d
        if 0 <= j < len(self.merge_files):
            self.merge_files[i], self.merge_files[j] = self.merge_files[j], self.merge_files[i]
            self._merge_refresh()
            self.merge_list.selection_set(j)

    def merge_remove(self):
        sel = self.merge_list.curselection()
        if sel:
            del self.merge_files[sel[0]]
            self._merge_refresh()

    def export_merge(self):
        files = [p for p, _ in self.merge_files]
        if len(files) < 2:
            messagebox.showinfo('Klip Kalkanı', 'En az iki klip ekle.')
            return
        problem = check_concat(files)
        if problem:
            messagebox.showerror('Klip Kalkanı', 'Bu klipler kayıpsız birleştirilemiyor:\n\n' + problem)
            return
        root, ext = os.path.splitext(files[0])
        dst = filedialog.asksaveasfilename(title='Birleşik klibi kaydet', initialdir=os.path.dirname(files[0]),
                                           initialfile=f'{os.path.basename(root)}_birlesik{ext}',
                                           defaultextension=ext, filetypes=VIDEO_TYPES)
        if not dst:
            return
        if os.path.normcase(os.path.abspath(dst)) in {os.path.normcase(os.path.abspath(f)) for f in files}:
            messagebox.showerror('Klip Kalkanı', 'Kaynak kliplerden birinin üstüne yazılamaz, başka bir ad seç.')
            return
        self.merge_msg.configure(text='Birleştiriliyor…')

        def done(ok, val):
            if ok:
                self.merge_msg.configure(text=f'✔ Kaydedildi: {os.path.basename(dst)} ({fmt_time(val)})')
            else:
                self._cleanup(dst)
                self.merge_msg.configure(text='İptal edildi.' if isinstance(val, Cancelled) else f'Hata: {val}')
        self._run_job(lambda p, c: concat_copy(files, dst, p, c), done, self.merge_bar, self.merge_msg,
                      [self.merge_btn, self.cut_btn])
