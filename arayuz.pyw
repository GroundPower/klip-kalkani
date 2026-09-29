# -*- coding: utf-8 -*-
"""Klip Kalkanı penceresi.

İlk açılışta kurulum (klip klasörleri, hız, Telegram girişi), sonra durum ekranı: ne yükleniyor,
duraklat/devam, hız ayarı, oyunlar, klasörler, geri yükleme ve klip düzenleme. Pencereyi kapatmak
yedeklemeyi durdurmaz; yedekleme arka planda (Görev Zamanlayıcı'daki "Klip Kalkani" görevi) çalışır.
"""

import asyncio
import base64
import collections
import ctypes
import datetime as dt
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import klip_kalkani as kk  # noqa: E402

try:
    import sv_ttk
except ImportError:  # tema yoksa düz ttk ile de çalışır
    sv_ttk = None

ICON_PATH = os.path.join(BASE, 'kalkan.ico')
FOLDERID_DESKTOP = '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}'
UNLIMITED = 100000
SPEED_CHOICES = ['Yükleme yok', '2', '5', '8', '10', '15', '20', '25', '30', '40', '50', '75', '100', 'Sınırsız']
NO_WINDOW = 0x08000000
GREEN, ORANGE, RED, BLUE, GREY = '#22c55e', '#f59e0b', '#ef4444', '#60a5fa', '#9ca3af'
MONTHS = ['Oca', 'Şub', 'Mar', 'Nis', 'May', 'Haz', 'Tem', 'Ağu', 'Eyl', 'Eki', 'Kas', 'Ara']


# ---------------------------------------------------------------- yardımcılar

def mbit_to_choice(m):
    if m is None or m >= UNLIMITED:
        return 'Sınırsız'
    if m <= 0:
        return 'Yükleme yok'
    return str(m)


def choice_to_mbit(s):
    s = (s or '').strip().lower()
    if s.startswith(('yük', 'yuk')):
        return 0
    if s.startswith(('sın', 'sin')):
        return UNLIMITED
    try:
        return max(0, int(float(s.replace(',', '.'))))
    except ValueError:
        return None


def plan_values(cfg):
    """(gündüz, gece) Mbit."""
    if not cfg.get('hiz_plani'):
        m = cfg.get('max_hiz_mbit') or 0
        return (m if m > 0 else UNLIMITED), (m if m > 0 else UNLIMITED)
    day, night = 8, 20
    for p in cfg['hiz_plani']:
        if p.get('baslangic') == '01:00':
            night = p.get('mbit', night)
        elif p.get('baslangic') == '09:00':
            day = p.get('mbit', day)
    return day, night


def make_plan(day, night):
    return [{'baslangic': '01:00', 'bitis': '09:00', 'mbit': night},
            {'baslangic': '09:00', 'bitis': '01:00', 'mbit': day}]


def update_config(**changes):
    cfg = kk.load_config()
    cfg.update(changes)
    kk.save_config(cfg)
    return cfg


def pct_text(p):
    """Türkçe yüzde: %0,03 · %4,5 · %45"""
    p = max(0.0, min(100.0, p))
    if p == 0 or p >= 10:
        s = f'{p:.0f}'
    elif p < 1:
        s = f'{p:.2f}'
    else:
        s = f'{p:.1f}'
    return '%' + s.replace('.', ',')


def date_text(ts):
    d = dt.datetime.fromtimestamp(ts)
    return f'{d.day} {MONTHS[d.month - 1]} {d.year}'


def human_ago(ts):
    return kk.fmt_duration(time.time() - ts) + ' önce' if ts else '—'


def ensure_icon():
    if os.path.exists(ICON_PATH):
        return ICON_PATH
    try:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(128, 12), (230, 48), (222, 150), (128, 246), (34, 150), (26, 48)], fill=(29, 78, 216, 255))
        d.polygon([(128, 34), (208, 62), (201, 146), (128, 222), (55, 146), (48, 62)], fill=(59, 130, 246, 255))
        d.polygon([(106, 86), (106, 170), (174, 128)], fill=(255, 255, 255, 255))
        img.save(ICON_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return ICON_PATH
    except Exception:
        return None


def make_desktop_shortcut():
    desktop = kk.known_folder(FOLDERID_DESKTOP) or os.path.join(os.path.expanduser('~'), 'Desktop')
    lnk = os.path.join(desktop, 'Klip Kalkanı.lnk')

    def q(s):
        return "'" + s.replace("'", "''") + "'"
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); "
              f"$s.TargetPath = {q(kk.pythonw_path())}; "
              f"$s.Arguments = {q('-E -s ' + chr(34) + os.path.abspath(__file__) + chr(34))}; "
              f"$s.WorkingDirectory = {q(BASE)}; "
              + (f"$s.IconLocation = {q(ICON_PATH)}; " if os.path.exists(ICON_PATH) else '')
              + "$s.Description = 'Klip Kalkanı'; $s.Save()")
    enc = base64.b64encode(script.encode('utf-16-le')).decode()
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', enc],
                   capture_output=True, creationflags=NO_WINDOW, timeout=60)
    return lnk


def collect_stats():
    """Veritabanından özet (arka planda çağrılır)."""
    cfg = kk.load_config()
    db = kk.open_db()
    try:
        since = kk.meta_get(db, 'son_tarama')
        done_n, done_b = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs "
                                    "WHERE status='done'").fetchone()
        pend, errs = {}, []
        nxt = []
        games = collections.defaultdict(lambda: [0, 0, 0, 0])  # klip, yedekli, boyut, yedekli boyut
        if since:
            for r in kk.pending_rows(db, since - 1):
                pend[r['fp'] or r['path']] = r['size']
                if r['attempts'] and r['last_error']:
                    errs.append(r['last_error'])
            nxt = [(r['path'], r['game'], r['size']) for r in kk.build_queue(db, cfg, since - 1)[:8]]
            seen = set()
            for game, key, size, done in db.execute(
                    "SELECT f.game, COALESCE(f.fp, f.path), f.size, b.status = 'done' FROM files f "
                    "LEFT JOIN blobs b ON b.fp = f.fp WHERE f.seen >= ? AND f.size > 0", (since - 1,)):
                if key in seen:
                    continue
                seen.add(key)
                g = games[game or 'Diğer']
                g[0] += 1
                g[2] += size
                if done:
                    g[1] += 1
                    g[3] += size
        partial = db.execute("SELECT COALESCE(SUM(p.length), 0) FROM parts p JOIN blobs b ON b.fp = p.fp "
                             "WHERE b.status != 'done'").fetchone()[0]
        midnight = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        today_b = db.execute('SELECT COALESCE(SUM(length), 0) FROM parts WHERE uploaded_at >= ?',
                             (midnight,)).fetchone()[0]
        stats = kk.meta_get(db, 'istatistik', {'bayt': 0, 'sn': 0})
        recent = db.execute('SELECT p.uploaded_at, p.length, p.idx, p.nparts, MIN(f.path), MIN(f.game) '
                            'FROM parts p JOIN files f ON f.fp = p.fp GROUP BY p.fp, p.idx '
                            'ORDER BY p.uploaded_at DESC LIMIT 20').fetchall()
        return {
            'done_n': done_n, 'done_b': done_b, 'pend_n': len(pend),
            'pend_b': max(0, sum(pend.values()) - partial), 'errors': errs,
            'avg': stats['bayt'] / stats['sn'] if stats.get('sn') else 0,
            'last_ok': kk.meta_get(db, 'son_basari'), 'unavailable': kk.meta_get(db, 'erisilemeyen') or [],
            'max_msg': db.execute('SELECT MAX(msg_id) FROM parts').fetchone()[0],
            'scanned': since is not None, 'today_b': today_b, 'next': nxt,
            'recent': [tuple(r) for r in recent], 'games': dict(games),
        }
    finally:
        db.close()


class Async:
    """Telegram işleri için ayrı iş parçacığında çalışan asyncio döngüsü."""

    def __init__(self, app):
        self.app = app
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, done=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if done:
            fut.add_done_callback(lambda f: self.app.post(done, f))
        return fut


class TelegramLogin:
    def __init__(self):
        self.client = None
        self.phone = None
        self.code_hash = None

    async def _client(self):
        from telethon import TelegramClient
        if self.client is None:
            cfg = kk.load_config()
            self.client = TelegramClient(
                kk.SESSION_BASE, int(cfg['api_id']), cfg['api_hash'], device_model='Klip Kalkani',
                system_version='Windows', app_version=kk.VERSION, lang_code='tr', system_lang_code='tr',
                receive_updates=False)
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def is_authorized(self):
        c = await self._client()
        return await c.is_user_authorized()

    async def send_code(self, phone):
        c = await self._client()
        if await c.is_user_authorized():
            return 'zaten'
        sent = await c.send_code_request(phone)
        self.phone, self.code_hash = phone, sent.phone_code_hash
        return 'kod'

    async def sign_in(self, code):
        from telethon import errors
        c = await self._client()
        try:
            await c.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
        except errors.SessionPasswordNeededError:
            return 'sifre'
        return 'tamam'

    async def password(self, pw):
        c = await self._client()
        await c.sign_in(password=pw)
        return 'tamam'

    async def finish(self):
        """Arşiv grubunu hazırlar (yoksa açar), bağlantıyı kapatır. Adı döndürür."""
        c = await self._client()
        me = await c.get_me()
        cfg = kk.load_config()
        await kk.ensure_archive(c, cfg)
        await c.disconnect()
        return f'{me.first_name or ""} {me.last_name or ""}'.strip(), bool(getattr(me, 'premium', False))


def friendly_error(e):
    name = type(e).__name__
    msgs = {
        'PhoneNumberInvalidError': 'Telefon numarası geçersiz. Başında + ve ülke koduyla yaz (örn. +905xxxxxxxxx).',
        'PhoneCodeInvalidError': 'Kod yanlış, tekrar dene.',
        'PhoneCodeExpiredError': 'Kodun süresi dolmuş. "Kod gönder"e tekrar bas.',
        'PasswordHashInvalidError': 'İki adımlı doğrulama şifresi yanlış.',
        'FloodWaitError': f'Telegram çok fazla deneme dedi, {getattr(e, "seconds", "?")} sn sonra tekrar dene.',
        'ApiIdInvalidError': 'API bilgileri (api_id/api_hash) geçersiz.',
        'NotLoggedIn': 'Telegram girişi yok. Durum sekmesinden girişi yenile.',
    }
    return msgs.get(name, f'{name}: {e}')


def make_tree(parent, columns, height=8, name_title='Klip', name_width=320):
    """Kaydırma çubuklu Treeview. columns: [(id, başlık, genişlik, hiza)]"""
    frame = ttk.Frame(parent)
    frame.pack(fill='both', expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='tree headings', height=height)
    tree.heading('#0', text=name_title)
    tree.column('#0', width=name_width, stretch=True)
    for cid, title, width, anchor in columns:
        tree.heading(cid, text=title)
        tree.column(cid, width=width, anchor=anchor, stretch=False)
    sb = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side='left', fill='both', expand=True)
    sb.pack(side='right', fill='y')
    return tree


# ---------------------------------------------------------------- pencere

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'Klip Kalkanı {kk.VERSION}')
        # Ekran ölçeklemesine (%125, %150…) ve ekran boyutuna göre pencere boyu; küçük ekranda taşmasın
        scale = max(1.0, self.winfo_fpixels('1i') / 96.0)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(int(1040 * scale), sw - 40), min(int(900 * scale), sh - 90)
        self.geometry(f'{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}')
        self.minsize(min(int(900 * scale), w), min(int(620 * scale), h))
        ico = ensure_icon()
        if ico:
            try:
                self.iconbitmap(ico)
            except tk.TclError:
                pass
        if sv_ttk:
            sv_ttk.set_theme('dark')
        style = ttk.Style(self)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 20))
        style.configure('H2.TLabel', font=('Segoe UI Semibold', 13))
        style.configure('Big.TLabel', font=('Segoe UI Semibold', 16))
        style.configure('Stat.TLabel', font=('Segoe UI Semibold', 13))
        style.configure('Pct.TLabel', font=('Segoe UI Semibold', 22), foreground=BLUE)
        style.configure('Muted.TLabel', foreground=GREY)
        style.configure('Big.TButton', font=('Segoe UI Semibold', 12), padding=(18, 10))
        self.uiq = queue.Queue()
        self.async_ = Async(self)
        self.after(100, self._poll_ui)
        self.view = None
        self.banner = None
        if self.needs_setup():
            self.show(SetupView)
        else:
            self.show(DashboardView)
        self.bg(self._update_check, self._update_done)

    # otomatik güncelleme (GitHub)
    @staticmethod
    def _update_check():
        m = kk.check_update()
        if not m:
            return None
        new = kk.apply_update(m)
        if kk.task_state() == 'acik' and kk.uploader_alive():
            # arka plandaki yükleyici de yeni kodla başlasın
            subprocess.run(['schtasks', '/End', '/TN', kk.TASK_NAME], capture_output=True, creationflags=NO_WINDOW)
            subprocess.run(['schtasks', '/Run', '/TN', kk.TASK_NAME], capture_output=True, creationflags=NO_WINDOW)
        return new

    def _update_done(self, res):
        ok, new = res
        if not ok or not new:
            return
        self.banner = ttk.Frame(self, padding=(20, 8))
        self.banner.pack(fill='x', before=self.view)
        ttk.Label(self.banner, text=f'✔ Klip Kalkanı {new} sürümüne güncellendi.', foreground=GREEN).pack(side='left')
        ttk.Button(self.banner, text='Yeni sürümü aç', style='Accent.TButton', command=self.restart).pack(side='right')

    def restart(self):
        subprocess.Popen([kk.pythonw_path(), '-E', '-s', os.path.abspath(__file__)], cwd=BASE)
        self.destroy()

    # iş parçacıklarından arayüze güvenli çağrı
    def post(self, fn, *args):
        self.uiq.put((fn, args))

    def _poll_ui(self):
        try:
            for _ in range(500):
                fn, args = self.uiq.get_nowait()
                try:
                    fn(*args)
                except Exception as e:  # arayüz çökmesin
                    print('UI hata:', e)
        except queue.Empty:
            pass
        self.after(100, self._poll_ui)

    def bg(self, fn, done, *args):
        def work():
            try:
                res = (True, fn(*args))
            except Exception as e:
                res = (False, e)
            self.post(done, res)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def needs_setup():
        c = kk.load_config()
        return not (os.path.exists(kk.SESSION_FILE) and c.get('kanal_id') and c.get('kaynaklar'))

    def show(self, view_cls):
        if self.view is not None:
            self.view.destroy()
        self.view = view_cls(self)
        self.view.pack(fill='both', expand=True)
        if self.banner is not None:
            self.banner.pack_forget()
            self.banner.pack(fill='x', before=self.view)


# ---------------------------------------------------------------- kurulum ekranı

class SetupView(ttk.Frame):
    def __init__(self, app):
        super().__init__(app, padding=(24, 18))
        self.app = app
        self.login = TelegramLogin()
        self.folder_vars = []   # (BooleanVar, src)
        self.logged_in = False

        ttk.Label(self, text=f'Klip Kalkanı kurulumu  (sürüm {kk.VERSION})', style='Title.TLabel').pack(anchor='w')
        ttk.Label(self, text='Oyun kliplerin Telegram\'da sadece senin göreceğin gizli bir gruba otomatik yedeklenir. '
                             'Hiçbir dosya silinmez.', style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 12))

        # 0) API bilgisi (sadece paketle gelmediyse sorulur)
        cfg = kk.load_config()
        self.need_api = not (cfg.get('api_id') and cfg.get('api_hash'))
        if self.need_api:
            box = ttk.LabelFrame(self, text=' 0. Telegram API bilgisi ', padding=12)
            box.pack(fill='x', pady=(0, 12))
            ttk.Label(box, text='my.telegram.org → telefonunla giriş → "API development tools" → herhangi bir ad ile '
                                'uygulama oluştur → api_id ve api_hash\'i buraya yapıştır (bir kere).',
                      style='Muted.TLabel', wraplength=880).pack(anchor='w')
            r = ttk.Frame(box)
            r.pack(fill='x', pady=(8, 0))
            ttk.Label(r, text='api_id:').pack(side='left')
            self.api_id = ttk.Entry(r, width=14)
            self.api_id.pack(side='left', padx=6)
            ttk.Label(r, text='  api_hash:').pack(side='left')
            self.api_hash = ttk.Entry(r, width=36)
            self.api_hash.pack(side='left', padx=6)
            ttk.Button(r, text='my.telegram.org\'u aç',
                       command=lambda: webbrowser.open('https://my.telegram.org/apps')).pack(side='left', padx=8)

        # 1) klasörler
        box = ttk.LabelFrame(self, text=' 1. Klip klasörleri ', padding=12)
        box.pack(fill='x')
        self.folder_frame = ttk.Frame(box)
        self.folder_frame.pack(fill='x')
        self.folder_msg = ttk.Label(self.folder_frame, text='Klasörler aranıyor…', style='Muted.TLabel')
        self.folder_msg.pack(anchor='w')
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='Klasör ekle…', command=self.add_folder).pack(side='left')

        # 2) hız
        box = ttk.LabelFrame(self, text=' 2. Yükleme hızı ', padding=12)
        box.pack(fill='x', pady=(12, 0))
        r = ttk.Frame(box)
        r.pack(fill='x')
        ttk.Label(r, text='Gündüz (09:00–01:00):').pack(side='left')
        self.day = ttk.Combobox(r, values=SPEED_CHOICES, width=12)
        self.day.set('8')
        self.day.pack(side='left', padx=(6, 18))
        ttk.Label(r, text='Gece (01:00–09:00):').pack(side='left')
        self.night = ttk.Combobox(r, values=SPEED_CHOICES, width=12)
        self.night.set('20')
        self.night.pack(side='left', padx=(6, 18))
        ttk.Label(r, text='Mbit').pack(side='left')
        r2 = ttk.Frame(box)
        r2.pack(fill='x', pady=(8, 0))
        self.measure_btn = ttk.Button(r2, text='İnternet hızımı ölç', command=self.measure)
        self.measure_btn.pack(side='left')
        self.speed_msg = ttk.Label(r2, text='Ölçünce uygun limitleri kendisi seçer. Oyun açıkken yükleme zaten durur.',
                                   style='Muted.TLabel')
        self.speed_msg.pack(side='left', padx=10)

        # 3) telegram
        box = ttk.LabelFrame(self, text=' 3. Telegram girişi ', padding=12)
        box.pack(fill='x', pady=(12, 0))
        r = ttk.Frame(box)
        r.pack(fill='x')
        ttk.Label(r, text='Telefon:').pack(side='left')
        self.phone = ttk.Entry(r, width=20)
        self.phone.insert(0, '+90')
        self.phone.pack(side='left', padx=6)
        self.send_btn = ttk.Button(r, text='Kod gönder', command=self.send_code)
        self.send_btn.pack(side='left')
        ttk.Label(r, text='   Kod:').pack(side='left')
        self.code = ttk.Entry(r, width=10, state='disabled')
        self.code.pack(side='left', padx=6)
        self.code_btn = ttk.Button(r, text='Giriş yap', command=self.sign_in, state='disabled')
        self.code_btn.pack(side='left')
        r2 = ttk.Frame(box)
        r2.pack(fill='x', pady=(8, 0))
        ttk.Label(r2, text='İki adımlı şifre (varsa):').pack(side='left')
        self.pw = ttk.Entry(r2, width=22, show='•', state='disabled')
        self.pw.pack(side='left', padx=6)
        self.pw_btn = ttk.Button(r2, text='Onayla', command=self.send_password, state='disabled')
        self.pw_btn.pack(side='left')
        self.tg_msg = ttk.Label(box, text='Kod, telefonundaki Telegram uygulamasına gelir.', style='Muted.TLabel')
        self.tg_msg.pack(anchor='w', pady=(8, 0))

        # bitir
        bottom = ttk.Frame(self)
        bottom.pack(fill='x', pady=(16, 0))
        self.shortcut = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text='Masaüstüne kısayol koy', variable=self.shortcut).pack(side='left')
        self.finish_btn = ttk.Button(bottom, text='Kurulumu bitir', style='Accent.TButton', command=self.finish,
                                     state='disabled')
        self.finish_btn.pack(side='right')
        self.finish_msg = ttk.Label(bottom, text='', style='Muted.TLabel')
        self.finish_msg.pack(side='right', padx=12)

        self.app.bg(self._detect, self._detected)
        if not self.need_api:
            self.app.async_.run(self.login.is_authorized(), self._auth_checked)

    # --- klasörler
    @staticmethod
    def _detect():
        out = []
        for label, s in kk.detect_clip_folders():
            n, size = kk.count_videos(s)
            out.append((label, s, n, size))
        return out

    def _detected(self, res):
        ok, val = res
        self.folder_msg.destroy()
        if not ok or not val:
            ttk.Label(self.folder_frame, text='Otomatik bir klip klasörü bulunamadı. "Klasör ekle" ile seç.',
                      style='Muted.TLabel').pack(anchor='w')
        else:
            for label, s, n, size in val:
                self._add_row(label, s, n, size, checked=n > 0)
        self._update_finish()

    def _add_row(self, label, s, n, size, checked=True):
        var = tk.BooleanVar(value=checked)
        text = f'{label}:  {s["yol"]}   ({kk.fmt_count(n)} video, {kk.fmt_size(size)})'
        ttk.Checkbutton(self.folder_frame, text=text, variable=var, command=self._update_finish).pack(anchor='w')
        self.folder_vars.append((var, s))

    def add_folder(self):
        p = filedialog.askdirectory(title='Klip klasörünü seç')
        if not p:
            return
        s = kk._src(os.path.normpath(p), 1)
        self.app.bg(kk.count_videos, lambda res: self._add_row(
            'Eklenen', s, *(res[1] if res[0] else (0, 0))) or self._update_finish(), s)

    # --- hız
    def measure(self):
        self.measure_btn.configure(state='disabled')
        self.speed_msg.configure(text='Ölçülüyor… (10–20 sn)')
        self.app.bg(kk.measure_upload_mbit, self._measured)

    def _measured(self, res):
        ok, val = res
        self.measure_btn.configure(state='normal')
        if not ok:
            self.speed_msg.configure(text=f'Ölçülemedi ({val}). Limitleri elle seçebilirsin.')
            return
        plan = kk.speed_plan_for(val)
        self.night.set(str(plan[0]['mbit']))
        self.day.set(str(plan[1]['mbit']))
        self.speed_msg.configure(text=f'Upload hızın ~{val:.0f} Mbit. Gündüz internetine yer kalsın diye daha düşük seçildi.')

    # --- telegram
    def _auth_checked(self, fut):
        try:
            if fut.result():
                self._logged_in('Bu bilgisayarda zaten giriş yapılmış.')
        except Exception as e:
            self.tg_msg.configure(text=f'Telegram\'a bağlanılamadı: {friendly_error(e)}')

    def send_code(self):
        phone = self.phone.get().strip().replace(' ', '')
        if len(phone) < 8:
            self.tg_msg.configure(text='Telefon numaranı başında + ile yaz (örn. +905xxxxxxxxx).')
            return
        if self.need_api:
            api_id, api_hash = self.api_id.get().strip(), self.api_hash.get().strip()
            if not api_id.isdigit() or len(api_hash) != 32:
                self.tg_msg.configure(text='Önce yukarıya api_id (sadece rakam) ve api_hash\'i (32 karakter) yaz.')
                return
            update_config(api_id=int(api_id), api_hash=api_hash)
        self.send_btn.configure(state='disabled')
        self.tg_msg.configure(text='Kod gönderiliyor…')
        self.app.async_.run(self.login.send_code(phone), self._code_sent)

    def _code_sent(self, fut):
        self.send_btn.configure(state='normal')
        try:
            r = fut.result()
        except Exception as e:
            self.tg_msg.configure(text=friendly_error(e))
            return
        if r == 'zaten':
            self._logged_in('Zaten giriş yapılmış.')
            return
        self.code.configure(state='normal')
        self.code_btn.configure(state='normal')
        self.code.focus_set()
        self.tg_msg.configure(text='Kod Telegram uygulamana gönderildi. Kodu yazıp "Giriş yap"a bas.')

    def sign_in(self):
        code = self.code.get().strip().replace(' ', '')
        if not code:
            return
        self.code_btn.configure(state='disabled')
        self.tg_msg.configure(text='Giriş yapılıyor…')
        self.app.async_.run(self.login.sign_in(code), self._signed_in)

    def _signed_in(self, fut):
        self.code_btn.configure(state='normal')
        try:
            r = fut.result()
        except Exception as e:
            self.tg_msg.configure(text=friendly_error(e))
            return
        if r == 'sifre':
            self.pw.configure(state='normal')
            self.pw_btn.configure(state='normal')
            self.pw.focus_set()
            self.tg_msg.configure(text='Hesabında iki adımlı doğrulama var. Şifreni yazıp "Onayla"ya bas.')
        else:
            self._logged_in('Giriş tamam.')

    def send_password(self):
        pw = self.pw.get()
        if not pw:
            return
        self.pw_btn.configure(state='disabled')
        self.app.async_.run(self.login.password(pw), self._pw_done)

    def _pw_done(self, fut):
        self.pw_btn.configure(state='normal')
        try:
            fut.result()
        except Exception as e:
            self.tg_msg.configure(text=friendly_error(e))
            return
        self._logged_in('Giriş tamam.')

    def _logged_in(self, text):
        self.logged_in = True
        for w in (self.phone, self.send_btn, self.code, self.code_btn, self.pw, self.pw_btn):
            w.configure(state='disabled')
        self.tg_msg.configure(text='✔ ' + text, foreground=GREEN)
        self._update_finish()

    # --- bitir
    def _update_finish(self):
        has_folder = any(v.get() for v, _ in self.folder_vars)
        self.finish_btn.configure(state='normal' if (has_folder and self.logged_in) else 'disabled')
        if not has_folder:
            self.finish_msg.configure(text='En az bir klasör seç.')
        elif not self.logged_in:
            self.finish_msg.configure(text='Telegram girişini yap.')
        else:
            self.finish_msg.configure(text='Hazır.')

    def finish(self):
        day, night = choice_to_mbit(self.day.get()), choice_to_mbit(self.night.get())
        if day is None or night is None:
            messagebox.showwarning('Klip Kalkanı', 'Hız limitini listeden seç ya da sayı yaz.')
            return
        sources = [s for v, s in self.folder_vars if v.get()]
        update_config(kaynaklar=sources, hiz_plani=make_plan(day, night), max_hiz_mbit=day, duraklat=False)
        self.finish_btn.configure(state='disabled')
        self.finish_msg.configure(text='Telegram\'da gizli arşiv grubu hazırlanıyor…')
        self.app.async_.run(self.login.finish(), self._archive_ready)

    def _archive_ready(self, fut):
        try:
            fut.result()
        except Exception as e:
            self.finish_btn.configure(state='normal')
            self.finish_msg.configure(text=friendly_error(e))
            return
        self.finish_msg.configure(text='Otomatik başlatma kuruluyor…')
        self.app.bg(self._install, self._installed)

    def _install(self):
        kk.register_task()
        if self.shortcut.get():
            make_desktop_shortcut()
        return True

    def _installed(self, res):
        ok, val = res
        if not ok:
            messagebox.showerror('Klip Kalkanı', f'Otomatik başlatma kurulamadı:\n{val}')
        messagebox.showinfo('Klip Kalkanı', 'Kurulum bitti! Yedekleme arka planda başladı.\n\n'
                                            'Bilgisayar her açıldığında kendiliğinden çalışır. Bu pencereyi '
                                            'kapatabilirsin; durumu görmek için tekrar açman yeter.')
        self.app.show(DashboardView)


# ---------------------------------------------------------------- ana ekran

class DashboardView(ttk.Frame):
    def __init__(self, app):
        super().__init__(app, padding=(20, 14))
        self.app = app
        self.stats = None
        self.stats_busy = False
        self._ts = (0, 'acik')

        head = ttk.Frame(self)
        head.pack(fill='x')
        ttk.Label(head, text='Klip Kalkanı', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text=f'  sürüm {kk.VERSION}', style='Muted.TLabel').pack(side='left', anchor='s', pady=(0, 6))
        self.bg_label = ttk.Label(head, text='', style='Muted.TLabel')
        self.bg_label.pack(side='right')

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, pady=(10, 0))
        self.tabs = {}
        for key, title in (('status', 'Durum'), ('games', 'Oyunlar'), ('speed', 'Hız'), ('folders', 'Klasörler'),
                           ('restore', 'Geri Yükle')):
            f = ttk.Frame(nb, padding=16)
            nb.add(f, text=f'  {title}  ')
            self.tabs[key] = f
        self.nb = nb
        nb.bind('<<NotebookTabChanged>>', self._tab_changed)

        self._build_status()
        self._build_games()
        self._build_speed()
        self._build_folders()
        self._build_restore()
        self.editor = None
        try:  # klip düzenleyici (önizleme, kesme, birleştirme)
            import duzenle
            f = ttk.Frame(nb, padding=12)
            nb.add(f, text='  Düzenle  ')
            self.tabs['edit'] = f
            self.editor = duzenle.EditorTab(f, app)
        except ImportError:
            pass
        self._tick()
        self._refresh_stats()

    def _tab_changed(self, _e):
        tab = self.nb.select()
        if tab == str(self.tabs['folders']):
            self.load_folders()
        elif tab == str(self.tabs['restore']):
            self.load_restore_choices()

    # ------------------------------------------------ Durum
    def _build_status(self):
        t = self.tabs['status']
        row = ttk.Frame(t)
        row.pack(fill='x')
        self.dot = tk.Label(row, text='●', font=('Segoe UI', 24), fg=GREY, bg=self._bg())
        self.dot.pack(side='left', anchor='n')
        col = ttk.Frame(row)
        col.pack(side='left', padx=(8, 0), fill='x', expand=True)
        self.state_label = ttk.Label(col, text='…', style='Big.TLabel')
        self.state_label.pack(anchor='w')
        self.state_sub = ttk.Label(col, text='', style='Muted.TLabel', wraplength=700)
        self.state_sub.pack(anchor='w')
        self.pause_btn = ttk.Button(row, text='⏸  Duraklat', style='Big.TButton', command=self.toggle_pause)
        self.pause_btn.pack(side='right')

        cur = ttk.LabelFrame(t, text=' Şu an yüklenen ', padding=12)
        cur.pack(fill='x', pady=(12, 0))
        top = ttk.Frame(cur)
        top.pack(fill='x')
        self.cur_name = ttk.Label(top, text='—', style='H2.TLabel')
        self.cur_name.pack(side='left')
        self.cur_pct = ttk.Label(top, text='', style='Pct.TLabel')
        self.cur_pct.pack(side='right')
        self.cur_meta = ttk.Label(cur, text='', style='Muted.TLabel')
        self.cur_meta.pack(anchor='w')
        self.cur_bar = ttk.Progressbar(cur, maximum=100)
        self.cur_bar.pack(fill='x', pady=(8, 4))
        self.cur_info = ttk.Label(cur, text='', style='Muted.TLabel')
        self.cur_info.pack(anchor='w')

        tot = ttk.LabelFrame(t, text=' Toplam ilerleme ', padding=12)
        tot.pack(fill='x', pady=(12, 0))
        top = ttk.Frame(tot)
        top.pack(fill='x')
        self.tot_title = ttk.Label(top, text='Hesaplanıyor…', style='H2.TLabel')
        self.tot_title.pack(side='left')
        self.tot_pct = ttk.Label(top, text='', style='Pct.TLabel')
        self.tot_pct.pack(side='right')
        self.tot_bar = ttk.Progressbar(tot, maximum=100)
        self.tot_bar.pack(fill='x', pady=(8, 8))
        grid = ttk.Frame(tot)
        grid.pack(fill='x')
        self.stat = {}
        cards = [('done_n', 'Yedeklenen klip'), ('pend_n', 'Kalan klip'), ('done_b', 'Yedeklenen boyut'),
                 ('pend_b', 'Kalan boyut'), ('today', 'Bugün yüklenen'), ('avg', 'Ortalama hız'),
                 ('eta', 'Tahmini bitiş'), ('last', 'Son yedek')]
        for i, (key, cap) in enumerate(cards):
            f = ttk.Frame(grid)
            f.grid(row=i // 4, column=i % 4, sticky='w', padx=(0, 20), pady=4)
            ttk.Label(f, text=cap, style='Muted.TLabel').pack(anchor='w')
            v = ttk.Label(f, text='—', style='Stat.TLabel')
            v.pack(anchor='w')
            self.stat[key] = v
        for c in range(4):
            grid.columnconfigure(c, weight=1)
        self.tot_warn = ttk.Label(tot, text='', foreground=ORANGE, wraplength=900, justify='left')
        self.tot_warn.pack(anchor='w', pady=(6, 0))

        btns = ttk.Frame(t)
        btns.pack(fill='x', pady=(12, 0))
        ttk.Button(btns, text="Telegram'da aç", command=self.open_telegram).pack(side='left')
        ttk.Button(btns, text='Günlüğü aç', command=self.open_log).pack(side='left', padx=8)
        ttk.Button(btns, text='Program klasörü', command=lambda: os.startfile(BASE)).pack(side='left')
        self.relogin_btn = ttk.Button(btns, text='Telegram girişini yenile', command=self.relogin)

        lists = ttk.Frame(t)
        lists.pack(fill='both', expand=True, pady=(12, 0))
        left = ttk.Frame(lists)
        left.pack(side='left', fill='both', expand=True, padx=(0, 8))
        right = ttk.Frame(lists)
        right.pack(side='left', fill='both', expand=True, padx=(8, 0))
        ttk.Label(left, text='Sıradakiler', style='H2.TLabel').pack(anchor='w', pady=(0, 6))
        self.next_tree = make_tree(left, [('oyun', 'Oyun', 140, 'w'), ('boyut', 'Boyut', 80, 'e')], height=6,
                                   name_width=220)
        ttk.Label(right, text='Son yedeklenenler', style='H2.TLabel').pack(anchor='w', pady=(0, 6))
        self.recent = make_tree(right, [('zaman', 'Ne zaman', 90, 'w'), ('boyut', 'Boyut', 80, 'e')], height=6,
                                name_width=220)

    def _bg(self):
        try:
            return ttk.Style(self).lookup('TFrame', 'background') or '#1c1c1c'
        except tk.TclError:
            return '#1c1c1c'

    def _tick(self):
        """Her saniye: durum.json'dan anlık bilgi."""
        cfg = kk.load_config()
        s = kk.read_json(kk.STATUS_PATH, {}) or {}
        alive = kk.uploader_alive()
        paused_flag = bool(cfg.get('duraklat'))
        self.pause_btn.configure(text='▶  Devam et' if paused_flag or not alive else '⏸  Duraklat')
        d = s.get('durum') if alive else None
        if not alive:
            if time.time() - self._ts[0] > 10:
                self._ts = (time.time(), kk.task_state())
            state = self._ts[1]
            if paused_flag:
                self._set_state(ORANGE, 'Duraklatıldı', 'Devam et\'e basınca kaldığı yerden sürer.')
            elif state == 'yok':
                self._set_state(RED, 'Arka plan kurulu değil', 'Devam et\'e basınca kurulur ve başlar.')
            elif state == 'kapali':
                self._set_state(RED, 'Kapalı', 'Otomatik başlatma kapalı. Devam et\'e basınca açılır.')
            else:
                self._set_state(GREY, 'Başlıyor…', 'Arka plan birkaç saniye içinde açılır.')
            self._set_current(None, s)
        elif d == 'yukleniyor':
            self._set_state(GREEN, 'Yükleniyor', f'Hız sınırı şu an: {self._limit_text(cfg)}. '
                                                 'Oyun açılınca kendiliğinden durur.')
            self._set_current(s.get('dosya'), s)
        elif d == 'duraklatildi':
            sebep = s.get('sebep') or ''
            if sebep == 'elle duraklatıldı':
                self._set_state(ORANGE, 'Duraklatıldı', 'Devam et\'e basınca kaldığı yerden sürer.')
            elif sebep.startswith('oyun'):
                self._set_state(ORANGE, 'Oyun açık, bekliyor', sebep + '. Oyun kapanınca 1 dk sonra devam eder.')
            else:
                self._set_state(ORANGE, 'Bekliyor', sebep)
            self._set_current(s.get('dosya'), s)
        elif d == 'hazir':
            self._set_state(GREEN, 'Her şey yedekli', 'Yeni klip gelince kendiliğinden yükler.')
            self._set_current(None, s)
        elif d == 'giris_gerekli':
            self._set_state(RED, 'Telegram girişi gerekli', s.get('sebep') or '')
            self.relogin_btn.pack(side='right')
            self._set_current(None, s)
        else:
            self._set_state(GREY, d or '…', s.get('sebep') or '')
        self.bg_label.configure(text=f'Arka plan: {"çalışıyor" if alive else "kapalı"}  ·  '
                                     f'Hız sınırı: {self._limit_text(cfg)}')
        self.after(1000, self._tick)

    @staticmethod
    def _limit_text(cfg):
        m = kk.planned_mbit(cfg)
        if m is None or m >= UNLIMITED:
            return 'sınırsız'
        return 'bu saatte yükleme yok' if m == 0 else f'{m} Mbit'

    def _set_state(self, color, text, sub):
        self.dot.configure(fg=color)
        self.state_label.configure(text=text)
        self.state_sub.configure(text=sub)

    def _set_current(self, path, s):
        if not path:
            self.cur_name.configure(text='—')
            self.cur_pct.configure(text='')
            self.cur_meta.configure(text='')
            self.cur_bar['value'] = 0
            self.cur_info.configure(text='')
            return
        part_size = max(1, s.get('parca_boyut') or 1)
        sent = min(part_size, s.get('gonderilen') or 0)
        total = s.get('dosya_boyut') or part_size
        done = (s.get('parca_ofset') or 0) + sent
        pct = 100 * done / max(1, total)
        self.cur_name.configure(text=os.path.basename(path))
        self.cur_pct.configure(text=pct_text(pct))
        self.cur_meta.configure(text=f'{s.get("oyun") or ""}  ·  {os.path.dirname(path)}')
        self.cur_bar['value'] = pct
        spd = s.get('hiz') or 0
        left = (total - done) / spd if spd > 1000 else None
        part = s.get('parca') or '1/1'
        self.cur_info.configure(text=f'{kk.fmt_size(done)} / {kk.fmt_size(total)}'
                                     + (f'  ·  {kk.fmt_size(spd)}/sn' if spd else '')
                                     + (f'  ·  ~{kk.fmt_duration(left)} kaldı' if left else '')
                                     + (f'  ·  parça {part}' if part != '1/1' else ''))

    def _refresh_stats(self):
        if not self.stats_busy:
            self.stats_busy = True
            self.app.bg(collect_stats, self._stats_ready)
        self.after(15000, self._refresh_stats)

    def _stats_ready(self, res):
        self.stats_busy = False
        ok, st = res
        if not ok:
            self.tot_title.configure(text=f'Özet okunamadı: {st}')
            return
        self.stats = st
        total_n = st['done_n'] + st['pend_n']
        total_b = st['done_b'] + st['pend_b']
        pct = 100 * st['done_b'] / total_b if total_b else 0
        self.tot_pct.configure(text=pct_text(pct))
        self.tot_title.configure(text=f'{kk.fmt_size(st["done_b"])} / {kk.fmt_size(total_b)} yedeklendi')
        self.tot_bar['value'] = pct
        self.stat['done_n'].configure(text=f'{kk.fmt_count(st["done_n"])} / {kk.fmt_count(total_n)}  '
                                           f'({pct_text(100 * st["done_n"] / total_n if total_n else 0)})')
        self.stat['pend_n'].configure(text=kk.fmt_count(st['pend_n']))
        self.stat['done_b'].configure(text=kk.fmt_size(st['done_b']))
        self.stat['pend_b'].configure(text=kk.fmt_size(st['pend_b']))
        self.stat['today'].configure(text=kk.fmt_size(st['today_b']))
        self.stat['avg'].configure(text=f'{kk.fmt_size(st["avg"])}/sn' if st['avg'] else '—')
        if st['pend_b'] and st['avg']:
            secs = st['pend_b'] / st['avg']
            self.stat['eta'].configure(text=f'{date_text(time.time() + secs)}  (~{kk.fmt_duration(secs)})')
        else:
            self.stat['eta'].configure(text='bitti' if not st['pend_b'] and st['scanned'] else '—')
        self.stat['last'].configure(text=human_ago(st['last_ok']))
        warn = []
        if st['errors']:
            warn.append(f'Tekrar denenecek: {len(st["errors"])} klip (son hata: {st["errors"][0][:100]})')
        if st['unavailable']:
            warn.append('Ulaşılamayan klasörler (disk takılı değil mi?): ' + ', '.join(st['unavailable'][:4]))
        if not st['scanned']:
            warn.append('Klasörler henüz taranmadı; arka plan açılınca taranır.')
        if st['pend_b'] and st['avg']:
            warn.append('Tahmini bitiş oyun aralarını ve bilgisayarın kapalı olduğu saatleri saymaz.')
        self.tot_warn.configure(text='\n'.join(warn), foreground=ORANGE if (st['errors'] or st['unavailable'])
                                else GREY)

        self.next_tree.delete(*self.next_tree.get_children())
        for path, game, size in st['next']:
            self.next_tree.insert('', 'end', text=os.path.basename(path), values=(game or 'Diğer', kk.fmt_size(size)))
        self.recent.delete(*self.recent.get_children())
        today = dt.date.today()
        for ts, length, idx, nparts, path, game in st['recent']:
            when = dt.datetime.fromtimestamp(ts)
            name = os.path.basename(path or '?') + (f'  (parça {idx + 1}/{nparts})' if nparts > 1 else '')
            self.recent.insert('', 'end', text=name, values=(
                when.strftime('%H:%M') if when.date() == today else when.strftime('%d.%m %H:%M'),
                kk.fmt_size(length)))
        self._fill_games(st['games'])

    def toggle_pause(self):
        cfg = kk.load_config()
        alive = kk.uploader_alive()
        if cfg.get('duraklat') or not alive:
            update_config(duraklat=False)
            if not alive:
                self.app.bg(self._ensure_running, lambda res: None)
            self._ts = (0, 'acik')
        else:
            update_config(duraklat=True)

    @staticmethod
    def _ensure_running():
        if kk.task_state() == 'yok':
            kk.register_task()
        else:
            kk.start_task()

    def open_telegram(self):
        cfg = kk.load_config()
        post = (self.stats or {}).get('max_msg') or cfg.get('readme_msg_id') or 1
        if cfg.get('kanal_id'):
            webbrowser.open(f'tg://privatepost?channel={cfg["kanal_id"]}&post={post}')

    @staticmethod
    def open_log():
        p = os.path.join(kk.LOG_DIR, 'klip_kalkani.log')
        if os.path.exists(p):
            os.startfile(p)

    def relogin(self):
        if messagebox.askyesno('Klip Kalkanı', 'Telegram girişini yenilemek için kurulum ekranı açılsın mı?\n'
                                              '(Klasörler ve yedekler aynen kalır.)'):
            try:
                os.replace(kk.SESSION_FILE, kk.SESSION_FILE + '.eski')
            except OSError:
                pass
            self.app.show(SetupView)

    # ------------------------------------------------ Oyunlar
    def _build_games(self):
        t = self.tabs['games']
        ttk.Label(t, text='Oyun oyun ilerleme', style='H2.TLabel').pack(anchor='w')
        ttk.Label(t, text='Telegram\'daki arşiv grubunda her oyunun kendi konusu var.', style='Muted.TLabel'
                  ).pack(anchor='w', pady=(2, 10))
        self.games_tree = make_tree(t, [('klip', 'Klip', 80, 'e'), ('yedekli', 'Yedekli', 80, 'e'),
                                        ('yuzde', 'İlerleme', 90, 'e'), ('boyut', 'Boyut', 100, 'e'),
                                        ('yboyut', 'Yedeklenen', 100, 'e')], height=20, name_title='Oyun',
                                    name_width=320)

    def _fill_games(self, games):
        sel = self.games_tree.selection()
        self.games_tree.delete(*self.games_tree.get_children())
        for game, (n, done, size, done_b) in sorted(games.items(), key=lambda x: -x[1][2]):
            self.games_tree.insert('', 'end', iid=game, text=game, values=(
                kk.fmt_count(n), kk.fmt_count(done), pct_text(100 * done_b / size if size else 0),
                kk.fmt_size(size), kk.fmt_size(done_b)))
        for iid in sel:
            if self.games_tree.exists(iid):
                self.games_tree.selection_add(iid)

    # ------------------------------------------------ Hız
    def _build_speed(self):
        t = self.tabs['speed']
        cfg = kk.load_config()
        day, night = plan_values(cfg)
        ttk.Label(t, text='Yükleme hızı sınırı', style='H2.TLabel').pack(anchor='w')
        ttk.Label(t, text='Yükleme internetinin upload tarafını kullanır. Gündüz sana yer kalsın diye düşük, '
                          'gece yüksek tutabilirsin.', style='Muted.TLabel', wraplength=880).pack(anchor='w', pady=(2, 12))
        g = ttk.Frame(t)
        g.pack(anchor='w')
        ttk.Label(g, text='Gündüz (09:00–01:00):').grid(row=0, column=0, sticky='w', pady=4)
        self.s_day = ttk.Combobox(g, values=SPEED_CHOICES, width=14)
        self.s_day.set(mbit_to_choice(day))
        self.s_day.grid(row=0, column=1, padx=8)
        ttk.Label(g, text='Mbit').grid(row=0, column=2, sticky='w')
        ttk.Label(g, text='Gece (01:00–09:00):').grid(row=1, column=0, sticky='w', pady=4)
        self.s_night = ttk.Combobox(g, values=SPEED_CHOICES, width=14)
        self.s_night.set(mbit_to_choice(night))
        self.s_night.grid(row=1, column=1, padx=8)
        ttk.Label(g, text='Mbit').grid(row=1, column=2, sticky='w')

        self.v_game = tk.BooleanVar(value=bool(cfg.get('oyunda_dur', True)))
        self.v_awake = tk.BooleanVar(value=bool(cfg.get('uyku_engelle', True)))
        ttk.Checkbutton(t, text='Oyun açıkken yüklemeyi tamamen durdur (ping bozulmasın)',
                        variable=self.v_game).pack(anchor='w', pady=(14, 2))
        ttk.Checkbutton(t, text='Yükleme sürerken bilgisayar uykuya geçmesin (prizdeyken)',
                        variable=self.v_awake).pack(anchor='w', pady=2)
        r = ttk.Frame(t)
        r.pack(anchor='w', pady=(16, 0))
        ttk.Button(r, text='Kaydet', style='Accent.TButton', command=self.save_speed).pack(side='left')
        self.m_btn = ttk.Button(r, text='İnternet hızımı ölç', command=self.measure)
        self.m_btn.pack(side='left', padx=8)
        self.speed_msg = ttk.Label(t, text='', style='Muted.TLabel', wraplength=880)
        self.speed_msg.pack(anchor='w', pady=(10, 0))

    def save_speed(self):
        day, night = choice_to_mbit(self.s_day.get()), choice_to_mbit(self.s_night.get())
        if day is None or night is None:
            self.speed_msg.configure(text='Listeden seç ya da sayı yaz (Mbit).')
            return
        update_config(hiz_plani=make_plan(day, night), max_hiz_mbit=day, oyunda_dur=self.v_game.get(),
                      uyku_engelle=self.v_awake.get())
        self.speed_msg.configure(text='✔ Kaydedildi. Arka plan birkaç saniye içinde yeni ayarla devam eder.',
                                 foreground=GREEN)

    def measure(self):
        self.m_btn.configure(state='disabled')
        self.speed_msg.configure(text='Ölçülüyor… (10–20 sn; yükleme sürüyorsa sonuç biraz düşük çıkabilir)',
                                 foreground=GREY)
        self.app.bg(kk.measure_upload_mbit, self._measured)

    def _measured(self, res):
        ok, val = res
        self.m_btn.configure(state='normal')
        if not ok:
            self.speed_msg.configure(text=f'Ölçülemedi: {val}')
            return
        plan = kk.speed_plan_for(val)
        self.s_night.set(str(plan[0]['mbit']))
        self.s_day.set(str(plan[1]['mbit']))
        self.speed_msg.configure(text=f'Upload ~{val:.0f} Mbit. Önerilen değerler seçildi; uygunsa Kaydet\'e bas.',
                                 foreground=GREY)

    # ------------------------------------------------ Klasörler
    def _build_folders(self):
        t = self.tabs['folders']
        ttk.Label(t, text='Yedeklenen klasörler', style='H2.TLabel').pack(anchor='w')
        ttk.Label(t, text='Buradan çıkardığın klasördeki dosyalar silinmez; o ana kadar yedeklenenler Telegram\'da '
                          'kalır. Sıra: önce yeni çekilen klipler, sonra sıra numarası küçük olan.',
                  style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 10))
        self.tree = make_tree(t, [('oncelik', 'Sıra', 50, 'e'), ('klip', 'Klip', 70, 'e'), ('boyut', 'Boyut', 90, 'e'),
                                  ('yedekli', 'Yedekli', 70, 'e'), ('bekleyen', 'Bekleyen', 80, 'e'),
                                  ('yuzde', 'İlerleme', 80, 'e')], height=14, name_title='Klasör', name_width=380)
        r = ttk.Frame(t)
        r.pack(fill='x', pady=(10, 0))
        ttk.Button(r, text='Klasör ekle…', command=self.add_folder).pack(side='left')
        ttk.Button(r, text='Seçileni çıkar', command=self.remove_folder).pack(side='left', padx=8)
        ttk.Button(r, text='Önce bunu yükle', command=self.prioritize).pack(side='left')
        ttk.Button(r, text='Yenile', command=self.load_folders).pack(side='right')
        self.folder_msg = ttk.Label(t, text='', style='Muted.TLabel')
        self.folder_msg.pack(anchor='w', pady=(8, 0))

    def load_folders(self):
        self.folder_msg.configure(text='Yükleniyor…')
        self.app.bg(self._folder_rows, self._folders_ready)

    @staticmethod
    def _folder_rows():
        cfg = kk.load_config()
        db = kk.open_db()
        try:
            since = kk.meta_get(db, 'son_tarama') or 0
            per, _ = kk.source_summary(db, cfg, since)
        finally:
            db.close()
        rows = []
        for s in sorted(cfg['kaynaklar'], key=lambda s: s.get('oncelik', 5)):
            n, size, ok, wait = per.get(s['yol'], [0, 0, 0, 0])
            rows.append((s['yol'], s.get('oncelik', 5), n, size, ok, wait))
        return rows

    def _folders_ready(self, res):
        ok, rows = res
        self.tree.delete(*self.tree.get_children())
        if not ok:
            self.folder_msg.configure(text=f'Okunamadı: {rows}')
            return
        for yol, prio, n, size, done, wait in rows:
            self.tree.insert('', 'end', iid=yol, text=yol, values=(
                prio, kk.fmt_count(n), kk.fmt_size(size) if size else '—', kk.fmt_count(done), kk.fmt_count(wait),
                pct_text(100 * done / n) if n else '—'))
        self.folder_msg.configure(text=f'{len(rows)} klasör. Yeni eklenenler en geç 10 dk içinde taranır.')

    def add_folder(self):
        p = filedialog.askdirectory(title='Yedeklenecek klip klasörünü seç')
        if not p:
            return
        p = os.path.normpath(p)
        cfg = kk.load_config()
        if any(os.path.normcase(s['yol']) == os.path.normcase(p) for s in cfg['kaynaklar']):
            self.folder_msg.configure(text='Bu klasör zaten listede.')
            return
        cfg['kaynaklar'].append(kk._src(p, 3))
        kk.save_config(cfg)
        self.load_folders()

    def remove_folder(self):
        sel = self.tree.selection()
        if not sel:
            self.folder_msg.configure(text='Önce listeden bir klasör seç.')
            return
        if not messagebox.askyesno('Klip Kalkanı', f'{sel[0]}\n\nBu klasör yedekleme listesinden çıkarılsın mı?\n'
                                                  '(Dosyalar silinmez, yedeklenmiş olanlar Telegram\'da kalır.)'):
            return
        cfg = kk.load_config()
        cfg['kaynaklar'] = [s for s in cfg['kaynaklar'] if s['yol'] != sel[0]]
        kk.save_config(cfg)
        self.load_folders()

    def prioritize(self):
        sel = self.tree.selection()
        if not sel:
            self.folder_msg.configure(text='Önce listeden bir klasör seç.')
            return
        cfg = kk.load_config()
        low = min((s.get('oncelik', 5) for s in cfg['kaynaklar']), default=1)
        for s in cfg['kaynaklar']:
            if s['yol'] == sel[0]:
                s['oncelik'] = max(0, low - 1)
        kk.save_config(cfg)
        self.load_folders()
        self.folder_msg.configure(text='Sıra değişti. Şu anki dosya bitince yeni sıraya geçer.')

    # ------------------------------------------------ Geri Yükle
    def _build_restore(self):
        t = self.tabs['restore']
        self.restore_future = None
        ttk.Label(t, text='Klipleri Telegram\'dan geri indir', style='H2.TLabel').pack(anchor='w')
        ttk.Label(t, text='Seçtiğin klipler hedef klasöre, oyun adına göre alt klasörlere iner; her parça orijinaliyle '
                          'karşılaştırılır. Telegram indirmeyi hesap başına ~6 MB/sn ile sınırlıyor.',
                  style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 12))
        g = ttk.Frame(t)
        g.pack(fill='x')
        ttk.Label(g, text='Hedef klasör:').grid(row=0, column=0, sticky='w', pady=4)
        self.r_target = ttk.Entry(g, width=58)
        self.r_target.insert(0, os.path.join(os.path.expanduser('~'), 'Videos', 'Geri Yuklenen Klipler'))
        self.r_target.grid(row=0, column=1, sticky='we', padx=8)
        ttk.Button(g, text='Seç…', command=self.pick_target).grid(row=0, column=2)
        ttk.Label(g, text='Oyun:').grid(row=1, column=0, sticky='w', pady=4)
        self.r_game = ttk.Combobox(g, values=['Hepsi'], width=40, state='readonly')
        self.r_game.set('Hepsi')
        self.r_game.grid(row=1, column=1, sticky='w', padx=8)
        ttk.Label(g, text='Ay:').grid(row=2, column=0, sticky='w', pady=4)
        self.r_month = ttk.Combobox(g, values=['Hepsi'], width=16, state='readonly')
        self.r_month.set('Hepsi')
        self.r_month.grid(row=2, column=1, sticky='w', padx=8)
        ttk.Label(g, text='Adında geçen:').grid(row=3, column=0, sticky='w', pady=4)
        self.r_text = ttk.Entry(g, width=30)
        self.r_text.grid(row=3, column=1, sticky='w', padx=8)
        g.columnconfigure(1, weight=1)
        for w in (self.r_game, self.r_month):
            w.bind('<<ComboboxSelected>>', lambda _e: self.preview_restore())
        self.r_text.bind('<KeyRelease>', lambda _e: self.preview_restore())
        r = ttk.Frame(t)
        r.pack(fill='x', pady=(14, 0))
        self.r_start = ttk.Button(r, text='İndir', style='Accent.TButton', command=self.start_restore)
        self.r_start.pack(side='left')
        self.r_stop = ttk.Button(r, text='Durdur', command=self.stop_restore, state='disabled')
        self.r_stop.pack(side='left', padx=8)
        ttk.Button(r, text='Hedef klasörü aç', command=self.open_target).pack(side='left')
        self.r_msg = ttk.Label(r, text='', style='Muted.TLabel')
        self.r_msg.pack(side='left', padx=12)

        prog = ttk.LabelFrame(t, text=' İndirme ', padding=12)
        prog.pack(fill='x', pady=(14, 0))
        top = ttk.Frame(prog)
        top.pack(fill='x')
        self.r_all_lbl = ttk.Label(top, text='Henüz başlatılmadı', style='H2.TLabel')
        self.r_all_lbl.pack(side='left')
        self.r_all_pct = ttk.Label(top, text='', style='Pct.TLabel')
        self.r_all_pct.pack(side='right')
        self.r_all_bar = ttk.Progressbar(prog, maximum=100)
        self.r_all_bar.pack(fill='x', pady=(6, 4))
        self.r_all_info = ttk.Label(prog, text='', style='Muted.TLabel')
        self.r_all_info.pack(anchor='w')
        self.r_cur_lbl = ttk.Label(prog, text='', style='Muted.TLabel')
        self.r_cur_lbl.pack(anchor='w', pady=(10, 0))
        self.r_cur_bar = ttk.Progressbar(prog, maximum=100)
        self.r_cur_bar.pack(fill='x', pady=(4, 0))
        ttk.Label(t, text='Biten klipler', style='H2.TLabel').pack(anchor='w', pady=(12, 6))
        self.r_log = make_tree(t, [('sonuc', 'Sonuç', 260, 'w')], height=6, name_width=420)

    def pick_target(self):
        p = filedialog.askdirectory(title='Kliplerin ineceği klasör')
        if p:
            self.r_target.delete(0, 'end')
            self.r_target.insert(0, os.path.normpath(p))

    def open_target(self):
        p = self.r_target.get().strip()
        if p and os.path.isdir(p):
            os.startfile(p)

    def load_restore_choices(self):
        def q():
            db = kk.open_db()
            try:
                rows = db.execute("SELECT DISTINCT f.game, f.taken FROM files f JOIN blobs b ON b.fp = f.fp "
                                  "WHERE b.status='done'").fetchall()
            finally:
                db.close()
            games = sorted({r[0] or 'Diğer' for r in rows}, key=str.lower)
            months = sorted({dt.datetime.fromtimestamp(r[1]).strftime('%Y-%m') for r in rows if r[1]}, reverse=True)
            return games, months

        def done(res):
            ok, val = res
            if ok:
                self.r_game.configure(values=['Hepsi'] + val[0])
                self.r_month.configure(values=['Hepsi'] + val[1])
            self.preview_restore()
        self.app.bg(q, done)

    def _filters(self):
        game = self.r_game.get()
        month = self.r_month.get()
        return (None if game == 'Hepsi' else game, None if month == 'Hepsi' else month,
                self.r_text.get().strip() or None)

    @staticmethod
    def _match(meta, game, month, text):
        if game and kk.norm_key(game) != kk.norm_key(meta['game']):
            return False
        if month and dt.datetime.fromtimestamp(meta['taken']).strftime('%Y-%m') != month:
            return False
        if text and text.lower() not in meta['name'].lower():
            return False
        return True

    def preview_restore(self):
        game, month, text = self._filters()

        def q():
            db = kk.open_db()
            try:
                rows = db.execute("SELECT f.fp, f.path, f.size, f.game, f.taken FROM files f JOIN blobs b "
                                  "ON b.fp = f.fp WHERE b.status='done'").fetchall()
            finally:
                db.close()
            sel = {}
            for fp, path, size, g, taken in rows:
                meta = {'game': g or 'Diğer', 'taken': taken or 0, 'name': os.path.basename(path)}
                if self._match(meta, game, month, text):
                    sel[fp] = size
            return len(sel), sum(sel.values())

        def done(res):
            ok, val = res
            if ok:
                self.r_msg.configure(text=f'{kk.fmt_count(val[0])} klip, {kk.fmt_size(val[1])} seçili')
        self.app.bg(q, done)

    def start_restore(self):
        if self.restore_future and not self.restore_future.done():
            return
        target = self.r_target.get().strip()
        if not target:
            self.r_msg.configure(text='Hedef klasörü seç.')
            return
        game, month, text = self._filters()
        if not (game or month or text) and not messagebox.askyesno(
                'Klip Kalkanı', 'Filtre seçilmedi: yedeklenen HER ŞEY indirilecek. Emin misin?'):
            return
        os.makedirs(target, exist_ok=True)
        self.r_log.delete(*self.r_log.get_children())
        self.rs = {'total': 0, 'done': 0, 'n': 0, 'i': 0, 'cur': '', 'cur_size': 1, 'cur_done': 0,
                   'window': collections.deque(), 'ok': 0, 'fail': 0}
        self.r_start.configure(state='disabled')
        self.r_stop.configure(state='normal')
        self.r_all_lbl.configure(text='Telegram grubu okunuyor…')
        self.restore_future = self.app.async_.run(self._restore_job(target, game, month, text), self._restore_end)
        self._restore_tick()

    async def _restore_job(self, target, game, month, text):
        cfg = kk.load_config()
        client = await kk.connect(cfg)
        try:
            groups = await kk.collect_channel(client, kk.channel_peer(cfg))
            sel = sorted((g for g in groups.values() if self._match(g['meta'], game, month, text)),
                         key=lambda g: g['meta']['taken'])
            self.app.post(self._r_begin, len(sel), sum(g['meta']['size'] for g in sel))
            for i, g in enumerate(sel, 1):
                m = g['meta']
                dest = os.path.join(target, kk.safe_name(m['game']), kk.safe_name(m['name']))
                self.app.post(self._r_clip, i, m['name'], m['size'])
                try:
                    r = await kk.download_group(client, g, dest,
                                                progress=lambda n: self.app.post(self._r_bytes, n))
                    self.app.post(self._r_clip_done, m['name'], m['size'], r)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.app.post(self._r_clip_fail, m['name'], f'{type(e).__name__}: {e}')
        finally:
            await client.disconnect()

    def _r_begin(self, n, total):
        self.rs.update(n=n, total=total)
        self.r_all_lbl.configure(text=f'{kk.fmt_count(n)} klip indirilecek ({kk.fmt_size(total)})')

    def _r_clip(self, i, name, size):
        self.rs.update(i=i, cur=name, cur_size=max(1, size), cur_done=0)

    def _r_bytes(self, n):
        self.rs['cur_done'] += n
        self.rs['done'] += n
        self.rs['window'].append((time.time(), n))

    def _r_clip_done(self, name, size, r):
        if r == 'var':
            self.rs['done'] += size
            text = 'zaten vardı, atlandı'
        elif r == 'eksik':
            text = 'EKSİK PARÇA, indirilemedi'
            self.rs['fail'] += 1
        else:
            text = '✔ indi, orijinaliyle aynı'
        self.rs['ok'] += r != 'eksik'
        self.r_log.insert('', 0, text=name, values=(text,))

    def _r_clip_fail(self, name, err):
        self.rs['fail'] += 1
        self.r_log.insert('', 0, text=name, values=(f'HATA: {err[:120]}',))

    def _restore_tick(self):
        if not self.restore_future:
            return
        rs = self.rs
        now = time.time()
        while rs['window'] and rs['window'][0][0] < now - 10:
            rs['window'].popleft()
        spd = sum(b for _, b in rs['window']) / 10 if rs['window'] else 0
        if rs['total']:
            pct = 100 * rs['done'] / rs['total']
            self.r_all_pct.configure(text=pct_text(pct))
            self.r_all_bar['value'] = pct
            left = (rs['total'] - rs['done']) / spd if spd > 1000 else None
            self.r_all_info.configure(text=f'{rs["i"]}/{rs["n"]} klip  ·  {kk.fmt_size(rs["done"])} / '
                                           f'{kk.fmt_size(rs["total"])}'
                                           + (f'  ·  {kk.fmt_size(spd)}/sn' if spd else '')
                                           + (f'  ·  ~{kk.fmt_duration(left)} kaldı' if left else ''))
        if rs['cur']:
            cp = 100 * min(1.0, rs['cur_done'] / rs['cur_size'])
            self.r_cur_lbl.configure(text=f'Şu an: {rs["cur"]}  ·  {pct_text(cp)}')
            self.r_cur_bar['value'] = cp
        if not self.restore_future.done():
            self.after(500, self._restore_tick)

    def _restore_end(self, fut):
        self.r_start.configure(state='normal')
        self.r_stop.configure(state='disabled')
        self._restore_tick()
        rs = self.rs
        if fut.cancelled():
            self.r_all_lbl.configure(text=f'Durduruldu. {rs["ok"]} klip indi.')
            return
        try:
            fut.result()
        except Exception as e:
            self.r_all_lbl.configure(text=f'Hata: {friendly_error(e)}')
            return
        self.r_all_lbl.configure(text=f'Bitti: {rs["ok"]} klip tamam, {rs["fail"]} sorunlu')
        if rs['total']:
            self.r_all_pct.configure(text=pct_text(100))
            self.r_all_bar['value'] = 100

    def stop_restore(self):
        if self.restore_future and not self.restore_future.done():
            self.restore_future.cancel()
            self.r_all_lbl.configure(text='Durduruluyor…')


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # yüksek çözünürlükte bulanık olmasın
    except Exception:
        pass
    app = App()
    view = app.view if isinstance(app.view, DashboardView) else None
    if view and '--sekme' in sys.argv:
        i = sys.argv.index('--sekme')
        if i + 1 < len(sys.argv) and sys.argv[i + 1] in view.tabs:
            view.nb.select(view.tabs[sys.argv[i + 1]])
    if view and view.editor and '--ac' in sys.argv:  # bir klibi doğrudan düzenleyicide aç
        i = sys.argv.index('--ac')
        if i + 1 < len(sys.argv) and os.path.isfile(sys.argv[i + 1]):
            view.nb.select(view.tabs['edit'])
            app.after(300, view.editor.open_file, sys.argv[i + 1])
    app.mainloop()


if __name__ == '__main__':
    main()
