# -*- coding: utf-8 -*-
"""Klip Kalkanı penceresi.

İlk açılışta kurulum (klip klasörleri, hız, Telegram girişi), sonra durum ekranı: ne yükleniyor,
duraklat/devam, hız ayarı, oyunlar, klasörler, geri yükleme, klip düzenleme ve ayarlar (Telegram hesabı,
çıkış, güncelleme). Bütün işler üstteki menüde de var (otomatik başlatma, kısayollar, tarama, doğrulama,
kaldırma…). Pencereyi kapatmak yedeklemeyi durdurmaz; yedekleme arka planda (Görev Zamanlayıcı'daki "Klip Kalkani"
görevi) çalışır. Pencere genelde yanındaki "Klip Kalkanı.exe" ile açılır.
"""

import asyncio
import base64
import collections
import ctypes
import datetime as dt
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import webbrowser
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import klip_kalkani as kk  # noqa: E402

try:
    import sv_ttk
except ImportError:  # tema yoksa düz ttk ile de çalışır
    sv_ttk = None

ICON_PATH = os.path.join(BASE, 'kalkan.ico')
LAUNCHER = os.path.join(BASE, 'Klip Kalkanı.exe')
SHORTCUT_NOTE = "Klip Kalkanı: oyun kliplerini Telegram'a yedekler"
FOLDERID_DESKTOP = '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}'
FOLDERID_PROGRAMS = '{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}'  # Başlat menüsü > Programlar
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
    """(gündüz, gece) Mbit. Plan [gece, gündüz] sırasıyla durur."""
    plan = cfg.get('hiz_plani') or []
    if len(plan) < 2:
        m = cfg.get('max_hiz_mbit') or 0
        return (m if m > 0 else UNLIMITED), (m if m > 0 else UNLIMITED)
    return plan[1].get('mbit', 8), plan[0].get('mbit', 20)


def plan_times(cfg):
    """((gece başı, sonu), (gündüz başı, sonu)); Gelişmiş ayarlardan değiştirilebilir."""
    plan = cfg.get('hiz_plani') or []
    if len(plan) < 2:
        return ('01:00', '09:00'), ('09:00', '01:00')
    return ((plan[0].get('baslangic', '01:00'), plan[0].get('bitis', '09:00')),
            (plan[1].get('baslangic', '09:00'), plan[1].get('bitis', '01:00')))


def make_plan(day, night, cfg=None, times=None):
    """Hız planı; saatler verilmezse ayarlardakiler (yoksa 01–09 gece, 09–01 gündüz) korunur."""
    (n0, n1), (d0, d1) = times or plan_times(cfg or {})
    return [{'baslangic': n0, 'bitis': n1, 'mbit': night}, {'baslangic': d0, 'bitis': d1, 'mbit': day}]


def save_track_name(key, value):
    """Düzenleyicide verilen ses kanalı adı (boşsa varsayılana döner)."""
    cfg = kk.load_config()
    names = dict(cfg.get('ses_adlari') or {})
    if value:
        names[key] = value
    else:
        names.pop(key, None)
    update_config(ses_adlari=names)


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


def account_text(h):
    """'Kaan (@kaan)' gibi."""
    s = h.get('ad') or '?'
    if h.get('kullanici'):
        s += f' (@{h["kullanici"]})'
    return s


def update_result_text(info):
    """Son güncelleme kontrolünün sonucu: (yazı, renk)."""
    when, ok, val = info
    at = dt.datetime.fromtimestamp(when).strftime('%H:%M')
    if not ok:
        return f'Kontrol edilemedi ({at}): {update_error_text(val)}', ORANGE
    if val['durum'] == 'guncel':
        return f'✔ Güncelsin, en son sürüm bu. (Son kontrol {at})', GREEN
    if val['durum'] == 'kuruldu':
        return f'✔ {val["surum"]} sürümü indirildi ve kuruldu. Üstteki "Yeni sürümü aç"a basınca geçer.', GREEN
    if val['durum'] == 'gelistirici':
        return (f'Bu bir geliştirici kopyası (git), kendini güncellemez. GitHub\'daki son sürüm: {val["uzak"]}. '
                f'(Son kontrol {at})'), GREY
    return 'Otomatik güncelleme bu kopyada kapalı.', GREY


def update_error_text(e):
    if isinstance(e, urllib.error.HTTPError):
        return f'GitHub {e.code} hatası verdi'
    if isinstance(e, OSError):  # URLError de buna dahil
        return 'GitHub\'a ulaşılamadı (internet bağlantısı?)'
    return str(e)


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


def shortcut_paths():
    """(masaüstü, Başlat menüsü) kısayolu."""
    desktop = kk.known_folder(FOLDERID_DESKTOP) or os.path.join(os.path.expanduser('~'), 'Desktop')
    programs = kk.known_folder(FOLDERID_PROGRAMS) or os.path.join(
        os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs')
    return os.path.join(desktop, 'Klip Kalkanı.lnk'), os.path.join(programs, 'Klip Kalkanı.lnk')


def make_shortcut(lnk):
    """Klip Kalkanı.exe'ye (yoksa pythonw + arayuz.pyw'ye) kısayol yapar."""
    def q(s):  # PowerShell tek tırnaklı metin (içindeki ' ikilenir)
        return "'" + s.replace("'", "''") + "'"
    if os.path.exists(LAUNCHER):
        target, arguments, icon = LAUNCHER, '', LAUNCHER + ',0'
    else:
        target, arguments = kk.pythonw_path(), '-E -s ' + chr(34) + os.path.abspath(__file__) + chr(34)
        icon = ICON_PATH if os.path.exists(ICON_PATH) else ''
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); "
              f"$s.TargetPath = {q(target)}; $s.Arguments = {q(arguments)}; $s.WorkingDirectory = {q(BASE)}; "
              + (f"$s.IconLocation = {q(icon)}; " if icon else '')
              + f"$s.Description = {q(SHORTCUT_NOTE)}; $s.Save()")
    enc = base64.b64encode(script.encode('utf-16-le')).decode()
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', enc],
                       capture_output=True, creationflags=NO_WINDOW, timeout=60)
    if r.returncode != 0 or not os.path.exists(lnk):
        raise RuntimeError(f'{lnk} yazılamadı')
    return lnk


def make_desktop_shortcut():
    return make_shortcut(shortcut_paths()[0])


def remove_shortcuts():
    """Programı kaldırırken: masaüstü ve Başlat menüsündeki Klip Kalkanı kısayolları."""
    gone = []
    for lnk in shortcut_paths():
        if os.path.exists(lnk):
            os.remove(lnk)
            gone.append(lnk)
    return gone


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


# ---------------------------------------------------------------- menü (koyu tema, ikonlu)

MENU_BG, MENU_FG, MENU_HOVER, MENU_LINE = '#1b1b1b', '#e6e6e6', '#2e2e2e', '#2d2d2d'
DROP_BG, DROP_ACTIVE, DROP_OFF = '#2b2b2b', '#2563eb', '#6b7280'
ICON_FONTS = (r'C:\Windows\Fonts\SegoeIcons.ttf', r'C:\Windows\Fonts\segmdl2.ttf')  # Windows 11 / 10
GLYPHS = {'pause': '\uE769', 'play': '\uE768', 'link': '\uE71B', 'pin': '\uE718', 'folder': '\uE8B7',
          'settings': '\uE713', 'doc': '\uE8A5', 'delete': '\uE74D', 'close': '\uE711', 'search': '\uE721',
          'check': '\uE73E', 'sync': '\uE895', 'send': '\uE724', 'contact': '\uE77B', 'signout': '\uF3B1',
          'update': '\uE777', 'notes': '\uE70B', 'help': '\uE897', 'info': '\uE946', 'cloud': '\uE753',
          'shield': '\uEA18', 'login': '\uE8FA', 'download': '\uE896', 'edit': '\uE70F', 'save': '\uE74E'}


class MenuBar(tk.Frame):
    """Koyu temaya uyan menü çubuğu (Windows'un beyaz menü çubuğu yerine). Menüler her açılışta o anki duruma göre
    yeniden doldurulur; sağda "Gelişmiş ayarlar"."""

    def __init__(self, app, menus):
        super().__init__(app, bg=MENU_BG)
        font = ('Segoe UI', 10)
        self.fillers, self.menus = {}, {}
        for label, fill in menus:
            mb = tk.Menubutton(self, text=label, bg=MENU_BG, fg=MENU_FG, activebackground=MENU_HOVER,
                               activeforeground=MENU_FG, relief='flat', bd=0, padx=12, pady=5, font=font,
                               cursor='hand2')
            menu = tk.Menu(mb, tearoff=False, bg=DROP_BG, fg=MENU_FG, activebackground=DROP_ACTIVE,
                           activeforeground='white', disabledforeground=DROP_OFF, bd=0, relief='flat',
                           activeborderwidth=0, font=font)
            menu.configure(postcommand=lambda m=menu, f=fill: self._fill(m, f))
            mb.configure(menu=menu)
            mb.pack(side='left')
            self.fillers[label], self.menus[label] = fill, menu
        self.adv = tk.Label(self, text='  Gelişmiş ayarlar ', image=app.icon('settings'), compound='left',
                            bg=MENU_BG, fg=MENU_FG, padx=10, pady=5, font=font, cursor='hand2')
        self.adv.pack(side='right', padx=(0, 6))
        self.adv.bind('<Button-1>', lambda _e: app.open_advanced())
        self.adv.bind('<Enter>', lambda _e: self.adv.configure(bg=MENU_HOVER))
        self.adv.bind('<Leave>', lambda _e: self.adv.configure(bg=MENU_BG))

    @staticmethod
    def _fill(menu, fill):
        menu.delete(0, 'end')
        fill(menu)

    def menu(self, label):
        """Menüyü o anki duruma göre doldurup döndürür (kısayollar ve testler için)."""
        self._fill(self.menus[label], self.fillers[label])
        return self.menus[label]


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
    """Pencereden Telegram girişi. Giriş ayrı bir oturum dosyasında yapılır, her şey bitince asıl yerine konur;
    yarım kalan bir giriş programı girişli sanmasın diye."""

    def __init__(self):
        self.client = None
        self.phone = None
        self.code_hash = None

    async def _client(self):
        if self.client is None:
            tmp = kk.LOGIN_SESSION_BASE + '.session'
            if os.path.exists(kk.SESSION_FILE) and not os.path.exists(tmp):
                shutil.copy2(kk.SESSION_FILE, tmp)  # bu bilgisayarda giriş zaten varsa onunla devam edilir
            self.client = kk.new_client(kk.LOGIN_SESSION_BASE, kk.load_config())
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

    async def finish(self, allow_new=True):
        """Arşiv grubunu hazırlar (allow_new ise gerekirse yenisini açar) ve girişi kaydeder.
        Dönüş: (durum, hesap). durum 'hazir', 'yeni' ya da 'erisim_yok' (o zaman bağlantı açık kalır)."""
        c = await self._client()
        me = await c.get_me()
        info = kk.account_info(me)
        state = await kk.ensure_archive(c, kk.load_config(), allow_new=allow_new)
        if state != 'erisim_yok':
            kk.remember_account(info)
            await c.disconnect()
            self.client = None
            kk.install_login_session()
        return state, info

    async def cancel(self):
        """Yanlış hesapla girildiyse bu girişi kapatır (Telegram'daki cihaz listesinden de düşer)."""
        c, self.client = self.client, None
        if c is not None:
            try:
                if not c.is_connected():
                    await c.connect()
                if await c.is_user_authorized():
                    await c.log_out()
            finally:
                if c.session is not None:
                    await c.disconnect()
        tmp = kk.LOGIN_SESSION_BASE + '.session'
        if os.path.exists(tmp):
            os.replace(tmp, tmp + '.eski')


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
        'ConnectionError': 'Telegram\'a bağlanılamadı; internet bağlantını kontrol et.',
        'TimeoutError': 'Telegram cevap vermedi; internet bağlantını kontrol edip tekrar dene.',
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
        self.ui_scale = scale
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
        self.update_info = None   # son güncelleme kontrolü: (zaman, başarılı mı, sonuç)
        self.update_busy = False
        self.updated_to = None    # bu pencere açıkken kurulan sürüm (pencere yeniden açılınca devreye girer)
        self.menu_update = False  # menüden istenen kontrol: sonucu kutuyla söylenir
        self.job = None           # menüden başlatılan Telegram işi (doğrulama / yeniden kurma)
        self.autostart = tk.BooleanVar(value=False)  # Windows açılınca otomatik başlat (görev açık mı)
        self.note = ttk.Label(self, text='', style='Muted.TLabel', padding=(20, 4))
        self._note_job = None
        self._icons = {}
        self.adv = None           # Gelişmiş ayarlar penceresi
        self._build_menu()
        for seq, fn in (('<Control-d>', self.menu_pause), ('<Control-D>', self.menu_pause),
                        ('<F5>', self.menu_scan), ('<Control-u>', self.menu_check_updates),
                        ('<Control-U>', self.menu_check_updates), ('<F1>', self.menu_help),
                        ('<Control-comma>', self.open_advanced)):
            self.bind(seq, lambda _e, fn=fn: self._shortcut(fn))
        self.show(self.first_view())
        self.check_updates()
        self._refresh_autostart()

    # güncelleme (GitHub): açılışta kendiliğinden, Ayarlar'daki düğmeyle elle
    def check_updates(self, manual=False):
        if self.update_busy:
            return
        self.update_busy = True
        self.bg(self._update_job, self._update_done, manual)
        self._notify_update()

    def _update_job(self, manual):
        if not manual:
            self._refresh_uploader()
        if not kk.GUNCELLEME_REPO:
            return {'durum': 'kapali'}
        if not manual and not kk.load_config().get('otomatik_guncelle', True):
            return None  # otomatik güncelleme kapalı: sadece elle
        if kk.is_dev_copy() and not manual:
            return None  # geliştirme kopyası açılışta hiç bakmaz
        m = kk.fetch_manifest()
        remote = m['surum']
        if kk.is_dev_copy():
            return {'durum': 'gelistirici', 'uzak': remote}
        if self.updated_to and kk._version_tuple(remote) <= kk._version_tuple(self.updated_to):
            return {'durum': 'kuruldu', 'surum': self.updated_to}
        if not kk.is_newer(remote):
            if kk.missing_files(m):
                kk.apply_update(m)  # sonradan eklenen program dosyası (ör. Klip Kalkanı.exe) tamamlanır
            return {'durum': 'guncel', 'uzak': remote}
        new = kk.apply_update(m)
        if kk.task_state() == 'acik' and kk.uploader_alive():
            kk.restart_task()  # arka plandaki yükleyici de yeni kodla başlasın
        return {'durum': 'kuruldu', 'surum': new}

    @staticmethod
    def _refresh_uploader():
        """Program dosyaları yenilenmişse (zip üstüne açılmış ya da güncellenmiş) ama arka plandaki yükleyici hâlâ
        eski kodla çalışıyorsa yeni sürümle yeniden başlatır. Otomatik başlatma kapalıysa (duraklatılmışsa) dokunmaz."""
        try:
            s = kk.read_json(kk.STATUS_PATH, {}) or {}
            if kk.uploader_alive() and s.get('surum') != kk.VERSION and kk.task_state() == 'acik':
                kk.restart_task()
        except Exception as e:
            print('yükleyici yenilenemedi:', e)

    def _update_done(self, res):
        self.update_busy = False
        ok, val = res
        if not (ok and val is None):
            self.update_info = (time.time(), ok, val)
        if ok and val and val['durum'] == 'kuruldu':
            self.updated_to = val['surum']
            self._show_banner(val['surum'])
        self._notify_update()
        if self.menu_update:
            if ok and val is None:  # açılıştaki kontrol bakmadan döndü; menüden istenen asıl kontrol şimdi
                self.check_updates(manual=True)
                return
            self.menu_update = False
            messagebox.showinfo('Klip Kalkanı', update_result_text(self.update_info)[0])

    def _notify_update(self):
        fn = getattr(self.view, 'show_update_info', None)
        if fn:
            fn()

    def _show_banner(self, new):
        if self.banner is not None:
            self.banner.destroy()
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
    def first_view():
        c = kk.load_config()
        if c.get('telegramsiz'):
            return DashboardView  # Telegram'sız kullanım: yedekleme kapalı, geri kalan her şey açık
        if not (c.get('kanal_id') and c.get('kaynaklar')):
            return SetupView
        # kurulu ama Telegram girişi yok (çıkış yapılmış): sadece giriş ekranı, ayarlar olduğu gibi kalır
        return DashboardView if os.path.exists(kk.SESSION_FILE) else LoginView

    def show(self, view_cls):
        if self.view is not None:
            self.view.destroy()
        self.view = view_cls(self)
        self.view.pack(fill='both', expand=True)
        if self.banner is not None:
            self.banner.pack_forget()
            self.banner.pack(fill='x', before=self.view)

    def say(self, text, color=GREY, secs=10):
        """Pencerenin altında kısa bilgi (menüden yapılan işler için). secs=0: kalıcı."""
        self.note.configure(text=text, foreground=color)
        self.note.pack(side='bottom', fill='x', before=self.view)
        if self._note_job:
            self.after_cancel(self._note_job)
        self._note_job = self.after(secs * 1000, self.note.pack_forget) if secs else None

    # ------------------------------------------------ menü
    def icon(self, name, off=False):
        """Windows'un simge fontundan (Segoe Fluent Icons / MDL2 Assets) küçük ikon; font yoksa boş."""
        key = (name, off)
        if key not in self._icons:
            size = max(16, round(16 * self.ui_scale))
            img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            path = next((f for f in ICON_FONTS if os.path.exists(f)), None)
            if path and name in GLYPHS:
                ImageDraw.Draw(img).text((size / 2, size / 2), GLYPHS[name], anchor='mm',
                                         font=ImageFont.truetype(path, size - 2), fill=DROP_OFF if off else MENU_FG)
            self._icons[key] = ImageTk.PhotoImage(img, master=self)
        return self._icons[key]

    def item(self, menu, label, ico, command=None, accel=None, enabled=True):
        menu.add_command(label=f'  {label}', image=self.icon(ico, off=not enabled), compound='left',
                         command=command, accelerator=accel or '', state='normal' if enabled else 'disabled')

    def _build_menu(self):
        self.menubar = MenuBar(self, [('Program', self._fill_program), ('Yedek', self._fill_backup),
                                      ('Hesap', self._fill_account), ('Yardım', self._fill_help)])
        self.menubar.pack(side='top', fill='x')
        tk.Frame(self, height=1, bg=MENU_LINE).pack(side='top', fill='x')

    def _modes(self):
        """(Durum ekranında mı, Telegram'sız mı)"""
        return isinstance(self.view, DashboardView), bool(kk.load_config().get('telegramsiz'))

    def _fill_program(self, m):
        dash, local = self._modes()
        if dash and local:
            self.item(m, "Telegram'a bağlan…", 'login', self.connect_telegram)
            m.add_separator()
        elif dash:
            paused = bool(kk.load_config().get('duraklat')) or not kk.uploader_alive()
            self.item(m, 'Yedeklemeye devam et' if paused else 'Yedeklemeyi duraklat', 'play' if paused else 'pause',
                      self.menu_pause, 'Ctrl+D')
            self.item(m, 'Windows açılınca otomatik başlat', 'check' if self.autostart.get() else 'blank',
                      self.menu_toggle_autostart)
            m.add_separator()
        self.item(m, 'Masaüstüne kısayol koy', 'link', lambda: self.menu_shortcut(0))
        self.item(m, 'Başlat menüsüne ekle', 'pin', lambda: self.menu_shortcut(1))
        m.add_separator()
        self.item(m, 'Program klasörünü aç', 'folder', lambda: os.startfile(BASE))
        self.item(m, 'Ayar klasörünü aç', 'folder', self.menu_open_data)
        self.item(m, 'Günlüğü aç', 'doc', self.menu_open_log)
        m.add_separator()
        self.item(m, 'Gelişmiş ayarlar…', 'settings', self.open_advanced, 'Ctrl+,')
        m.add_separator()
        self.item(m, 'Programı kaldır…', 'delete', self.menu_uninstall)
        self.item(m, 'Kapat', 'close', self.destroy, 'Alt+F4')

    def _fill_backup(self, m):
        dash, local = self._modes()
        tg = dash and not local
        self.item(m, 'Klasörleri şimdi tara', 'search', self.menu_scan, 'F5', enabled=dash)
        self.item(m, "Telegram'daki yedekleri doğrula", 'shield', self.menu_verify, enabled=tg)
        self.item(m, "Kayıtları Telegram'dan yeniden kur…", 'sync', self.menu_reindex, enabled=tg)
        m.add_separator()
        self.item(m, "Arşiv grubunu Telegram'da aç", 'send', lambda: self._on_dashboard('open_telegram'), enabled=tg)
        self.item(m, 'Klipleri geri yükle…', 'download', lambda: self._select_tab('restore'), enabled=tg)
        self.item(m, 'Klip düzenle…', 'edit', lambda: self._select_tab('edit'), enabled=dash)

    def _fill_account(self, m):
        dash, local = self._modes()
        h = kk.load_config().get('hesap')
        if dash and not local:
            self.item(m, account_text(h) if h else 'Telegram hesabı', 'contact', enabled=False)
            m.add_separator()
            self.item(m, 'Hesap ayrıntıları', 'info', lambda: self._select_tab('settings'))
            self.item(m, "Telegram'dan çıkış yap…", 'signout', lambda: self._on_dashboard('logout'))
        else:
            self.item(m, "Telegram'a bağlı değil", 'contact', enabled=False)
            m.add_separator()
            self.item(m, "Telegram'a bağlan…", 'login', self.connect_telegram, enabled=dash)

    def _fill_help(self, m):
        self.item(m, 'Güncellemeleri kontrol et', 'update', self.menu_check_updates, 'Ctrl+U')
        self.item(m, 'Sürüm notları', 'notes', lambda: webbrowser.open(
            f'https://github.com/{kk.GUNCELLEME_REPO}/releases'))
        self.item(m, 'Nasıl kullanılır', 'help', self.menu_help, 'F1')
        m.add_separator()
        self.item(m, 'Hakkında', 'info', self.menu_about)

    def _shortcut(self, fn):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, tk.Text)):
            return None  # yazı yazılırken kısayollar karışmasın
        fn()
        return 'break'

    def _on_dashboard(self, method):
        if isinstance(self.view, DashboardView):
            getattr(self.view, method)()

    def _select_tab(self, key):
        if isinstance(self.view, DashboardView) and key in self.view.tabs:
            self.view.nb.select(self.view.tabs[key])

    def connect_telegram(self):
        """Telegram'sız kullanımdan yedeklemeye geçiş: giriş ekranı."""
        self.show(LoginView)

    def open_advanced(self):
        if self.adv is not None and self.adv.winfo_exists():
            self.adv.deiconify()
            self.adv.lift()
            self.adv.focus_set()
        else:
            self.adv = AdvancedSettings(self)
        return self.adv

    def menu_pause(self):
        dash, local = self._modes()
        if dash and not local:
            self.view.toggle_pause()

    def menu_toggle_autostart(self):
        self.autostart.set(not self.autostart.get())
        self.toggle_autostart()

    # otomatik başlatma (menüde ve Ayarlar sekmesinde aynı anahtar)
    def _refresh_autostart(self):
        self.bg(kk.task_state, self._autostart_state)
        self.after(20000, self._refresh_autostart)

    def _autostart_state(self, res):
        ok, state = res
        if ok:
            self.autostart.set(state == 'acik')

    def toggle_autostart(self):
        if self.autostart.get():
            self.bg(self._autostart_on, self._autostart_done)
            return
        if not messagebox.askyesno('Klip Kalkanı', 'Otomatik başlatma kapatılsın mı?\n\nArka plandaki yedekleme '
                                   'durur ve Windows açılınca kendiliğinden başlamaz. Tekrar açmak için yine buradan '
                                   'ya da Durum sekmesindeki "Devam et"ten açabilirsin.'):
            self.autostart.set(True)
            return
        self.bg(kk.stop_backup, self._autostart_done)

    @staticmethod
    def _autostart_on():
        if kk.task_state() == 'yok':
            kk.register_task()
        else:
            kk.start_task()

    def _autostart_done(self, res):
        ok, val = res
        if not ok:
            messagebox.showerror('Klip Kalkanı', f'Olmadı: {val}')
        self.bg(kk.task_state, self._autostart_state)
        if isinstance(self.view, DashboardView):
            self.view._ts = (0, 'acik')  # durum yazısı hemen tazelensin
        self.say('✔ Otomatik başlatma açıldı.' if self.autostart.get() else 'Otomatik başlatma kapatıldı.')

    def menu_shortcut(self, which):
        lnk = shortcut_paths()[which]

        def done(res):
            ok, val = res
            self.say('✔ Kısayol kondu: ' + val if ok else f'Kısayol konamadı: {val}', GREEN if ok else RED)
        self.bg(make_shortcut, done, lnk)

    def menu_open_data(self):
        os.makedirs(kk.DATA_DIR, exist_ok=True)
        os.startfile(kk.DATA_DIR)

    def menu_open_log(self):
        p = os.path.join(kk.LOG_DIR, 'klip_kalkani.log')
        if os.path.exists(p):
            os.startfile(p)
        elif os.path.isdir(kk.LOG_DIR):
            os.startfile(kk.LOG_DIR)
        else:
            self.say('Henüz günlük yok; arka plan ilk çalıştığında yazılır.')

    # yedek işleri
    def _busy(self):
        if self.job is not None and not self.job.done():
            self.say('Başka bir iş sürüyor, bitmesini bekle.', ORANGE)
            return True
        return False

    def menu_scan(self):
        if not isinstance(self.view, DashboardView):
            return
        self.say('Klasörler taranıyor…', secs=0)
        self.bg(self._scan, self._scanned)

    @staticmethod
    def _scan():
        res = kk.scan(kk.load_config())
        if kk.uploader_alive():
            kk.request_scan()  # arka plan da sırasını tazelesin
        return res

    def _scanned(self, res):
        ok, st = res
        if not ok:
            self.say(f'Taranamadı: {st}', RED)
            return
        text = f'✔ Tarandı: {kk.fmt_count(st["found"])} klip, {st["new"]} yeni, {st["changed"]} değişmiş.'
        if st['unavailable']:
            text += '  Ulaşılamayan: ' + ', '.join(st['unavailable'][:3])
        self.say(text, ORANGE if st['unavailable'] else GREEN, secs=20)
        if isinstance(self.view, DashboardView):
            self.view.refresh_now()

    def menu_verify(self):
        if self._busy():
            return
        self.say("Telegram'daki yedekler kontrol ediliyor…", secs=0)

        def progress(i, n):
            self.post(self.say, f'Kontrol ediliyor… {kk.fmt_count(i)} / {kk.fmt_count(n)} parça', GREY, 0)
        self.job = self.async_.run(kk.verify_archive(kk.load_config(), progress), self._verified)

    def _verified(self, fut):
        try:
            total, bad = fut.result()
        except Exception as e:
            self.say(f'Doğrulanamadı: {friendly_error(e)}', RED, secs=0)
            return
        if bad:
            self.say(f"{kk.fmt_count(total)} parçadan {bad} tanesi Telegram'da eksik/bozuk çıktı; tekrar yüklenecek.",
                     ORANGE, secs=0)
        else:
            self.say(f"✔ {kk.fmt_count(total)} parçanın hepsi Telegram'da sağlam duruyor.", GREEN, secs=30)

    def menu_reindex(self):
        if self._busy() or not messagebox.askyesno(
                'Klip Kalkanı', "Kayıtlar Telegram'daki arşivden yeniden okunsun mu?\n\nBilgisayar değiştiyse ya da "
                "program neyin yüklendiğini unuttuysa işe yarar. Telegram'da zaten olan klipler tekrar yüklenmez. "
                'Klip sayısına göre birkaç dakika sürebilir.'):
            return
        self.say("Telegram'daki arşiv okunuyor…", secs=0)
        self.job = self.async_.run(kk.reindex(kk.load_config()), self._reindexed)

    def _reindexed(self, fut):
        try:
            found, complete = fut.result()
        except Exception as e:
            self.say(f'Olmadı: {friendly_error(e)}', RED, secs=0)
            return
        self.say(f"✔ Telegram'da {kk.fmt_count(found)} klip bulundu, {kk.fmt_count(complete)} tanesi eksiksiz; "
                 'kayıtlar güncellendi.', GREEN, secs=30)
        if isinstance(self.view, DashboardView):
            self.view.refresh_now()

    # kaldırma
    def menu_uninstall(self):
        view = self.view
        if isinstance(view, DashboardView) and view.restore_future and not view.restore_future.done():
            messagebox.showinfo('Klip Kalkanı', 'Önce Geri Yükle sekmesindeki indirmeyi durdur.')
            return
        if not messagebox.askyesno('Klip Kalkanı', 'Klip Kalkanı bu bilgisayardan kaldırılsın mı?\n\n'
                                   '• Arka plandaki yedekleme durur, otomatik başlatma silinir.\n'
                                   '• Masaüstü ve Başlat menüsü kısayolları kaldırılır.\n'
                                   '• Telegram\'daki yedeklere ve bilgisayardaki kliplere dokunulmaz.', icon='warning'):
            return
        logout = os.path.exists(kk.SESSION_FILE) and messagebox.askyesno(
            'Klip Kalkanı', 'Bu bilgisayardaki Telegram girişi de kapatılsın mı?\n\n(Önerilir. Telegram\'daki '
                            'klipler yine silinmez; tekrar kurarsan yeniden giriş yaparsın.)')
        self.say('Kaldırılıyor…', secs=0)
        self.bg(self._uninstall, self._uninstalled, logout)

    @staticmethod
    def _uninstall(logout):
        kk.stop_uploader()
        kk.remove_task()
        gone = remove_shortcuts()
        if logout:
            asyncio.run(kk.telegram_logout())
        return gone

    def _uninstalled(self, res):
        ok, val = res
        if not ok:
            self.say(f'Kaldırılamadı: {val}', RED, secs=0)
            return
        messagebox.showinfo('Klip Kalkanı', 'Kaldırıldı: arka plan durdu, otomatik başlatma ve kısayollar silindi.\n\n'
                                            f'Program klasörünü istersen şimdi silebilirsin:\n{BASE}\n\n'
                                            'Ayarlar ve yedek kayıtları şurada duruyor; tekrar kurarsan kaldığı yerden '
                                            f'devam eder. Tamamen temizlemek istersen bunu da silebilirsin:\n{kk.DATA_DIR}')
        self.destroy()

    # yardım
    def menu_check_updates(self):
        self.menu_update = True
        self.check_updates(manual=True)  # zaten bakılıyorsa sonuç gelince söylenir

    @staticmethod
    def menu_help():
        readme = os.path.join(BASE, 'BENİ OKU.txt')
        if os.path.exists(readme):
            os.startfile(readme)
        else:
            webbrowser.open(f'https://github.com/{kk.GUNCELLEME_REPO}#readme')

    @staticmethod
    def menu_about():
        messagebox.showinfo('Klip Kalkanı', f'Klip Kalkanı {kk.VERSION}\n\nOyun kliplerini Telegram\'da sadece senin '
                                            'göreceğin gizli bir gruba yedekler. Hiçbir dosyayı silmez.\n\n'
                                            f'Program: {BASE}\nAyarlar: {kk.DATA_DIR}\n\n'
                                            f'github.com/{kk.GUNCELLEME_REPO}')


# ---------------------------------------------------------------- Telegram girişi

class LoginBox(ttk.LabelFrame):
    """Telefon → Telegram'a gelen kod → (varsa) iki adımlı şifre."""

    def __init__(self, parent, app, title, on_login, before_send=None):
        super().__init__(parent, text=title, padding=12)
        self.app = app
        self.on_login = on_login
        self.before_send = before_send
        self.login = TelegramLogin()
        self.logged_in = False
        r = ttk.Frame(self)
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
        r2 = ttk.Frame(self)
        r2.pack(fill='x', pady=(8, 0))
        ttk.Label(r2, text='İki adımlı şifre (varsa):').pack(side='left')
        self.pw = ttk.Entry(r2, width=22, show='•', state='disabled')
        self.pw.pack(side='left', padx=6)
        self.pw_btn = ttk.Button(r2, text='Onayla', command=self.send_password, state='disabled')
        self.pw_btn.pack(side='left')
        self.msg = ttk.Label(self, text='Kod, telefonundaki Telegram uygulamasına gelir.', style='Muted.TLabel')
        self.msg.pack(anchor='w', pady=(8, 0))
        self.phone.bind('<Return>', lambda _e: self.send_code())
        self.code.bind('<Return>', lambda _e: self.sign_in())
        self.pw.bind('<Return>', lambda _e: self.send_password())

    def say(self, text, color=GREY):
        self.msg.configure(text=text, foreground=color)

    def check_existing(self):
        """Bu bilgisayarda giriş zaten varsa (ya da yarım kalan giriş tamamlanmışsa) doğrudan devam eder."""
        self.app.async_.run(self.login.is_authorized(), self._auth_checked)

    def _auth_checked(self, fut):
        if not self.winfo_exists():  # bu arada ekran değişmiş
            return
        try:
            if fut.result():
                self._logged_in('Bu bilgisayarda zaten giriş yapılmış.')
        except Exception as e:
            self.say(f'Telegram\'a bağlanılamadı: {friendly_error(e)}')

    def send_code(self):
        if self.send_btn.instate(['disabled']):
            return
        phone = self.phone.get().strip().replace(' ', '')
        if len(phone) < 8:
            self.say('Telefon numaranı başında + ile yaz (örn. +905xxxxxxxxx).')
            return
        if self.before_send and not self.before_send():
            return
        self.send_btn.configure(state='disabled')
        self.say('Kod gönderiliyor…')
        self.app.async_.run(self.login.send_code(phone), self._code_sent)

    def _code_sent(self, fut):
        self.send_btn.configure(state='normal')
        try:
            r = fut.result()
        except Exception as e:
            self.say(friendly_error(e), RED)
            return
        if r == 'zaten':
            self._logged_in('Zaten giriş yapılmış.')
            return
        self.code.configure(state='normal')
        self.code_btn.configure(state='normal')
        self.code.focus_set()
        self.say('Kod Telegram uygulamana gönderildi. Kodu yazıp "Giriş yap"a bas.')

    def sign_in(self):
        code = self.code.get().strip().replace(' ', '')
        if not code or self.code_btn.instate(['disabled']):
            return
        self.code_btn.configure(state='disabled')
        self.say('Giriş yapılıyor…')
        self.app.async_.run(self.login.sign_in(code), self._signed_in)

    def _signed_in(self, fut):
        self.code_btn.configure(state='normal')
        try:
            r = fut.result()
        except Exception as e:
            self.say(friendly_error(e), RED)
            return
        if r == 'sifre':
            self.pw.configure(state='normal')
            self.pw_btn.configure(state='normal')
            self.pw.focus_set()
            self.say('Hesabında iki adımlı doğrulama var. Şifreni yazıp "Onayla"ya bas.')
        else:
            self._logged_in('Giriş tamam.')

    def send_password(self):
        pw = self.pw.get()
        if not pw or self.pw_btn.instate(['disabled']):
            return
        self.pw_btn.configure(state='disabled')
        self.app.async_.run(self.login.password(pw), self._pw_done)

    def _pw_done(self, fut):
        self.pw_btn.configure(state='normal')
        try:
            fut.result()
        except Exception as e:
            self.say(friendly_error(e), RED)
            return
        self._logged_in('Giriş tamam.')

    def _logged_in(self, text):
        self.logged_in = True
        for w in (self.phone, self.send_btn, self.code, self.code_btn, self.pw, self.pw_btn):
            w.configure(state='disabled')
        self.say('✔ ' + text, GREEN)
        self.on_login()

    def reset(self, text):
        """Baştan (başka bir hesapla) giriş için."""
        self.login = TelegramLogin()
        self.logged_in = False
        for w in (self.code, self.pw):
            w.configure(state='normal')
            w.delete(0, 'end')
            w.configure(state='disabled')
        for w in (self.code_btn, self.pw_btn):
            w.configure(state='disabled')
        for w in (self.phone, self.send_btn):
            w.configure(state='normal')
        self.say(text, ORANGE)


class ApiBox(ttk.LabelFrame):
    """Telegram API bilgisi: paketle gelmediyse bir kere sorulur (my.telegram.org)."""

    def __init__(self, parent, title=' Telegram API bilgisi '):
        super().__init__(parent, text=title, padding=12)
        ttk.Label(self, text='my.telegram.org → telefonunla giriş → "API development tools" → herhangi bir ad ile '
                             'uygulama oluştur → api_id ve api_hash\'i buraya yapıştır (bir kere).',
                  style='Muted.TLabel', wraplength=880).pack(anchor='w')
        r = ttk.Frame(self)
        r.pack(fill='x', pady=(8, 0))
        ttk.Label(r, text='api_id:').pack(side='left')
        self.api_id = ttk.Entry(r, width=14)
        self.api_id.pack(side='left', padx=6)
        ttk.Label(r, text='  api_hash:').pack(side='left')
        self.api_hash = ttk.Entry(r, width=36)
        self.api_hash.pack(side='left', padx=6)
        ttk.Button(r, text='my.telegram.org\'u aç',
                   command=lambda: webbrowser.open('https://my.telegram.org/apps')).pack(side='left', padx=8)

    def save(self, box):
        api_id, api_hash = self.api_id.get().strip(), self.api_hash.get().strip()
        if not api_id.isdigit() or len(api_hash) != 32:
            box.say('Önce api_id (sadece rakam) ve api_hash\'i (32 karakter) yaz.', ORANGE)
            return False
        update_config(api_id=int(api_id), api_hash=api_hash)
        return True


LOCAL_NOTE = ('Klipler yedeklenmez; düzenleyici, klasörler ve oyun özetleri çalışır. İstediğin zaman '
              'Hesap > Telegram\'a bağlan ile yedeklemeyi açabilirsin.')


class LoginView(ttk.Frame):
    """Kurulu programda sadece Telegram'a yeniden giriş (çıkış yaptıktan ya da oturum kapandıktan sonra).
    Klasörler, hız ayarları ve yedek kayıtları olduğu gibi kalır."""

    def __init__(self, app):
        super().__init__(app, padding=(24, 18))
        self.app = app
        self.new_archive = False
        cfg = kk.load_config()
        self.was_local = bool(cfg.get('telegramsiz'))
        h = cfg.get('hesap')
        if h and not self.was_local:
            ttk.Label(self, text='Telegram girişi', style='Title.TLabel').pack(anchor='w')
            ttk.Label(self, text='Klasörlerin, hız ayarların ve Telegram\'daki yedeklerin olduğu gibi duruyor; '
                                 'yedeklemenin sürmesi için tekrar giriş yapman yeterli.',
                      style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 0))
            ttk.Label(self, text=f'Arşiv grubu şu hesapta: {account_text(h)}. Aynı hesapla girersen kaldığı yerden '
                                 'devam eder.', style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 0))
        else:
            ttk.Label(self, text='Telegram\'a bağlan', style='Title.TLabel').pack(anchor='w')
            ttk.Label(self, text='Klipleri Telegram\'da sadece senin göreceğin gizli bir gruba yedeklemek için giriş yap. '
                                 'Klasörlerin ve ayarların olduğu gibi kalır.',
                      style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 0))
        self.api = None
        if not (cfg.get('api_id') and cfg.get('api_hash')):
            self.api = ApiBox(self)
            self.api.pack(fill='x', pady=(14, 0))
        self.box = LoginBox(self, app, ' Telegram hesabı ', self._on_login,
                            (lambda: self.api.save(self.box)) if self.api else None)
        self.box.pack(fill='x', pady=(14, 0))
        bottom = ttk.Frame(self)
        bottom.pack(fill='x', pady=(14, 0))
        self.local_btn = ttk.Button(bottom, text='Telegram olmadan devam et', command=self.continue_local)
        self.local_btn.pack(side='right')
        self.msg = ttk.Label(bottom, text='', style='Muted.TLabel', wraplength=640, justify='left')
        self.msg.pack(side='left')
        self.retry_btn = ttk.Button(bottom, text='Tekrar dene', command=self._on_login)
        if self.api is None:
            self.box.check_existing()

    def continue_local(self):
        if not messagebox.askyesno('Klip Kalkanı', 'Telegram olmadan devam edilsin mi?\n\n' + LOCAL_NOTE):
            return
        update_config(telegramsiz=True)
        self.app.show(DashboardView)

    def _on_login(self):
        self.retry_btn.pack_forget()
        self.msg.configure(text='Arşiv grubu kontrol ediliyor…', foreground=GREY)
        self.app.async_.run(self.box.login.finish(allow_new=False), self._checked)

    def _checked(self, fut):
        try:
            state, info = fut.result()
        except Exception as e:
            self.msg.configure(text=f'Olmadı: {friendly_error(e)}', foreground=RED)
            self.retry_btn.pack(side='right', padx=8)
            return
        if state == 'erisim_yok':
            if messagebox.askyesno('Klip Kalkanı', f'{account_text(info)} hesabında Klip Kalkanı arşiv grubu yok; '
                                   'yedeklerin başka bir hesapta duruyor olabilir.\n\n'
                                   'Bu hesapta yeni bir arşiv grubu açılsın ve bütün klipler baştan buraya '
                                   'yüklensin mi?\n\n"Hayır" dersen bu hesaptan çıkılır; doğru hesapla tekrar '
                                   'girebilirsin.'):
                self.msg.configure(text='Yeni arşiv grubu açılıyor…', foreground=GREY)
                self.app.async_.run(self.box.login.finish(allow_new=True), self._checked)
            else:
                self.msg.configure(text='Bu hesaptan çıkılıyor…', foreground=GREY)
                self.app.async_.run(self.box.login.cancel(), self._cancelled)
            return
        self.new_archive = state == 'yeni'
        update_config(telegramsiz=False)  # Telegram'sız kullanımdan yedeklemeye geçildi
        self.msg.configure(text='Yedekleme başlatılıyor…', foreground=GREY)
        self.app.bg(self._resume, self._resumed)

    def _cancelled(self, fut):
        try:
            fut.result()
        except Exception as e:
            print('çıkış:', e)
        self.msg.configure(text='')
        self.box.reset('Çıkış yapıldı. Doğru hesabın numarasıyla tekrar giriş yap.')

    @staticmethod
    def _resume():
        """Yükleyici eski oturumla bekliyorsa kapatıp yeni girişle başlatır. Duraklatılmışsa öyle kalır."""
        kk.stop_uploader()
        if kk.load_config().get('duraklat'):
            return False
        if kk.task_state() == 'yok':
            kk.register_task()
        else:
            kk.start_task()
        return True

    def _resumed(self, res):
        ok, started = res
        first = self.new_archive and (self.was_local or not kk.load_config().get('eski_kanallar'))
        if first:
            text = 'Telegram\'a bağlandın; gizli arşiv grubu açıldı.'
            if not kk.load_config().get('kaynaklar'):
                text += ' Klasörler sekmesinden klip klasörlerini eklemeyi unutma.'
        else:
            text = ('Yeni arşiv grubu açıldı; klipler bu hesaba baştan yüklenecek.' if self.new_archive
                    else 'Giriş tamam.')
        if not ok:
            text += f'\n\nOtomatik başlatma açılamadı: {started}'
        elif not started:
            text += ' Yedekleme duraklatılmış durumda; Durum sekmesindeki "Devam et" ile sürdürebilirsin.'
        elif not self.new_archive:
            text += ' Yedekleme kaldığı yerden devam ediyor.'
        messagebox.showinfo('Klip Kalkanı', text)
        self.app.show(DashboardView)


# ---------------------------------------------------------------- kurulum ekranı

class SetupView(ttk.Frame):
    def __init__(self, app):
        super().__init__(app, padding=(24, 18))
        self.app = app
        self.folder_vars = []   # (BooleanVar, src)
        self.logged_in = False

        ttk.Label(self, text=f'Klip Kalkanı kurulumu  (sürüm {kk.VERSION})', style='Title.TLabel').pack(anchor='w')
        ttk.Label(self, text='Oyun kliplerin Telegram\'da sadece senin göreceğin gizli bir gruba otomatik yedeklenir. '
                             'Hiçbir dosya silinmez.', style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(2, 12))

        # önceki kurulum (başka klasörde, 1.7 öncesi): ayarlarını al, kurulum gerekmesin
        old = kk.old_install()
        if old and not kk._same(old, BASE):
            box = ttk.LabelFrame(self, text=' Önceki kurulum bulundu ', padding=12)
            box.pack(fill='x', pady=(0, 12))
            ttk.Label(box, text=f'Bu bilgisayarda daha önce kurulmuş bir Klip Kalkanı var: {old}\nAyarlarını, Telegram '
                                'girişini ve yedek kayıtlarını buraya alırsan kurulum gerekmez; kaldığı yerden devam '
                                'eder.', wraplength=880, justify='left').pack(anchor='w')
            ttk.Button(box, text='Ayarlarımı buraya al', style='Accent.TButton',
                       command=lambda: self.adopt(old)).pack(anchor='w', pady=(8, 0))

        # 0) API bilgisi (sadece paketle gelmediyse sorulur)
        cfg = kk.load_config()
        self.need_api = not (cfg.get('api_id') and cfg.get('api_hash'))
        self.api = None
        if self.need_api:
            self.api = ApiBox(self, ' 0. Telegram API bilgisi ')
            self.api.pack(fill='x', pady=(0, 12))

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
        self.box = LoginBox(self, app, ' 3. Telegram girişi ', self._on_login, self._save_api)
        self.box.pack(fill='x', pady=(12, 0))
        ttk.Label(self, text='Telegram\'ın yoksa ya da şimdilik istemiyorsan aşağıdaki "Telegram olmadan devam et" ile '
                             'sadece düzenleyiciyi, klasörleri ve oyun özetlerini kullanabilirsin.',
                  style='Muted.TLabel', wraplength=900).pack(anchor='w', pady=(6, 0))

        # bitir
        bottom = ttk.Frame(self)
        bottom.pack(fill='x', pady=(16, 0))
        self.shortcut = tk.BooleanVar(value=True)
        self.start_menu = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text='Masaüstüne kısayol koy', variable=self.shortcut).pack(side='left')
        ttk.Checkbutton(bottom, text='Başlat menüsüne ekle', variable=self.start_menu).pack(side='left', padx=(12, 0))
        self.finish_btn = ttk.Button(bottom, text='Kurulumu bitir', style='Accent.TButton', command=self.finish,
                                     state='disabled')
        self.finish_btn.pack(side='right')
        self.local_btn = ttk.Button(bottom, text='Telegram olmadan devam et', command=self.finish_local)
        self.local_btn.pack(side='right', padx=(0, 8))
        self.finish_msg = ttk.Label(bottom, text='', style='Muted.TLabel')
        self.finish_msg.pack(side='right', padx=12)

        self.app.bg(self._detect, self._detected)
        if not self.need_api:
            self.box.check_existing()

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
    def _save_api(self):
        """Kod istenmeden önce: API bilgisi paketle gelmediyse kutulardan alınır."""
        return self.api.save(self.box) if self.api is not None else True

    def finish_local(self):
        """Telegram'a bağlanmadan: seçili klasörler ve hız kaydedilir, yedekleme (arka plan görevi) kurulmaz."""
        if not messagebox.askyesno('Klip Kalkanı', 'Telegram\'a bağlanmadan devam edilsin mi?\n\n' + LOCAL_NOTE):
            return
        day, night = choice_to_mbit(self.day.get()), choice_to_mbit(self.night.get())
        day, night = (8 if day is None else day), (20 if night is None else night)
        update_config(kaynaklar=[s for v, s in self.folder_vars if v.get()], hiz_plani=make_plan(day, night),
                      max_hiz_mbit=day, telegramsiz=True)
        self.app.bg(self._shortcuts, self._local_done, self.shortcut.get(), self.start_menu.get())

    @staticmethod
    def _shortcuts(desktop, start_menu):
        for want, lnk in zip((desktop, start_menu), shortcut_paths()):
            if want:
                make_shortcut(lnk)
        return True

    def _local_done(self, res):
        ok, val = res
        if not ok:
            messagebox.showerror('Klip Kalkanı', f'Kısayol konamadı:\n{val}')
        self.app.show(DashboardView)

    def _on_login(self):
        self.logged_in = True
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
        self.app.async_.run(self.box.login.finish(), self._archive_ready)

    def _archive_ready(self, fut):
        try:
            fut.result()
        except Exception as e:
            self.finish_btn.configure(state='normal')
            self.finish_msg.configure(text=friendly_error(e))
            return
        self.finish_msg.configure(text='Otomatik başlatma kuruluyor…')
        self.app.bg(self._install, self._installed, self.shortcut.get(), self.start_menu.get())

    @staticmethod
    def _install(desktop, start_menu):
        kk.register_task()
        for want, lnk in zip((desktop, start_menu), shortcut_paths()):
            if want:
                make_shortcut(lnk)
        return True

    # --- başka klasördeki eski kurulum
    def adopt(self, old):
        if not messagebox.askyesno('Klip Kalkanı', f'{old} klasöründeki ayarlar, Telegram girişi ve yedek kayıtları '
                                   'ayar klasörüne taşınsın ve arka plan bundan sonra bu kopyadan çalışsın mı?\n\n'
                                   'Eski klasördeki program dosyalarına dokunulmaz; sonra istersen silebilirsin.'):
            return
        self.app.bg(self._adopt, self._adopted, old)

    @staticmethod
    def _adopt(old):
        kk.adopt_old_install(old)
        for lnk in shortcut_paths():  # eski klasörü gösteren kısayollar buraya dönsün
            if os.path.exists(lnk):
                make_shortcut(lnk)
        return True

    def _adopted(self, res):
        ok, val = res
        if not ok:
            messagebox.showerror('Klip Kalkanı', f'Ayarlar alınamadı:\n{val}')
            return
        self.app.show(self.app.first_view())

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
        self._tick_job = self._stats_job = None
        self._acc_loaded = self._acc_busy = False

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
            self.editor = duzenle.EditorTab(f, app, names_get=lambda: kk.load_config().get('ses_adlari') or {},
                                            names_set=save_track_name)
        except ImportError:
            pass
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text='  Ayarlar  ')
        self.tabs['settings'] = f
        self._build_settings()
        self._tick()
        self._refresh_stats()
        if kk.load_config().get('telegramsiz'):  # yükleyici çalışmıyor: klasörleri pencere tarar
            self.app.bg(self._local_scan, lambda res: self.refresh_now())

    @staticmethod
    def _local_scan():
        db = kk.open_db()
        try:
            last = kk.meta_get(db, 'son_tarama')
        finally:
            db.close()
        if last is None or time.time() - last > 600:
            return kk.scan(kk.load_config())

    def destroy(self):
        """Çıkış yapınca ekran değişir: zamanlayıcıları ve açık klibin oynatıcısını kapat."""
        for job in (self._tick_job, self._stats_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self.editor is not None:
            self.editor.close()
        super().destroy()

    def _tab_changed(self, _e):
        tab = self.nb.select()
        if tab == str(self.tabs['folders']):
            self.load_folders()
        elif tab == str(self.tabs['restore']):
            self.load_restore_choices()
        elif tab == str(self.tabs['settings']):
            self.load_account()

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
        if cfg.get('telegramsiz'):
            self.pause_btn.configure(text='☁  Telegram\'a bağlan')
            self._set_state(GREY, 'Telegram\'sız kullanıyorsun', LOCAL_NOTE)
            self._set_current(None, {})
            self.bg_label.configure(text='Telegram\'sız kullanım · yedekleme kapalı')
            self._tick_job = self.after(1000, self._tick)
            return
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
        self._tick_job = self.after(1000, self._tick)

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
        self.refresh_now()
        self._stats_job = self.after(15000, self._refresh_stats)

    def refresh_now(self):
        if not self.stats_busy:
            self.stats_busy = True
            self.app.bg(collect_stats, self._stats_ready)

    def _stats_ready(self, res):
        self.stats_busy = False
        if not self.winfo_exists():  # bu arada ekran değişmiş (ör. çıkış yapıldı)
            return
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
        if cfg.get('telegramsiz'):
            self.app.connect_telegram()
            return
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
        if messagebox.askyesno('Klip Kalkanı', 'Telegram girişini yenilemek için giriş ekranı açılsın mı?\n'
                                              '(Klasörler, ayarlar ve yedekler aynen kalır.)'):
            try:
                os.replace(kk.SESSION_FILE, kk.SESSION_FILE + '.eski')
            except OSError:
                pass
            self.app.show(LoginView)

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
        (n0, n1), (d0, d1) = plan_times(cfg)
        self.lbl_day = ttk.Label(g, text=f'Gündüz ({d0}–{d1}):')
        self.lbl_day.grid(row=0, column=0, sticky='w', pady=4)
        self.s_day = ttk.Combobox(g, values=SPEED_CHOICES, width=14)
        self.s_day.set(mbit_to_choice(day))
        self.s_day.grid(row=0, column=1, padx=8)
        ttk.Label(g, text='Mbit').grid(row=0, column=2, sticky='w')
        self.lbl_night = ttk.Label(g, text=f'Gece ({n0}–{n1}):')
        self.lbl_night.grid(row=1, column=0, sticky='w', pady=4)
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

    def refresh_settings(self):
        """Gelişmiş ayarlar kaydedilince Hız sekmesi de güncellensin."""
        cfg = kk.load_config()
        day, night = plan_values(cfg)
        (n0, n1), (d0, d1) = plan_times(cfg)
        self.s_day.set(mbit_to_choice(day))
        self.s_night.set(mbit_to_choice(night))
        self.lbl_day.configure(text=f'Gündüz ({d0}–{d1}):')
        self.lbl_night.configure(text=f'Gece ({n0}–{n1}):')
        self.v_game.set(bool(cfg.get('oyunda_dur', True)))
        self.v_awake.set(bool(cfg.get('uyku_engelle', True)))

    def save_speed(self):
        day, night = choice_to_mbit(self.s_day.get()), choice_to_mbit(self.s_night.get())
        if day is None or night is None:
            self.speed_msg.configure(text='Listeden seç ya da sayı yaz (Mbit).')
            return
        update_config(hiz_plani=make_plan(day, night, kk.load_config()), max_hiz_mbit=day,
                      oyunda_dur=self.v_game.get(), uyku_engelle=self.v_awake.get())
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

    # ------------------------------------------------ Ayarlar
    def _build_settings(self):
        t = self.tabs['settings']
        box = ttk.LabelFrame(t, text=' Telegram hesabı ', padding=12)
        box.pack(fill='x')
        r = ttk.Frame(box)
        r.pack(fill='x')
        self.acc_label = ttk.Label(r, text='—', style='H2.TLabel')
        self.acc_label.pack(side='left')
        self.logout_btn = ttk.Button(r, text='Çıkış yap', command=self.logout)
        self.logout_btn.pack(side='right')
        self.acc_sub = ttk.Label(box, text='', style='Muted.TLabel', wraplength=880, justify='left')
        self.acc_sub.pack(anchor='w', pady=(4, 0))
        self.acc_note = ttk.Label(box, text='Çıkış yapınca yedekleme durur ve bu bilgisayardaki giriş silinir; '
                                            'Telegram\'daki arşiv grubu ve klipler olduğu gibi kalır. Aynı hesapla '
                                            'tekrar girince kaldığı yerden devam eder.',
                                  style='Muted.TLabel', wraplength=880, justify='left')
        self.acc_note.pack(anchor='w', pady=(8, 0))
        cfg = kk.load_config()
        if cfg.get('telegramsiz'):
            self.acc_label.configure(text='Telegram\'a bağlı değil')
            self.acc_sub.configure(text=LOCAL_NOTE, foreground=GREY)
            self.logout_btn.configure(text='Telegram\'a bağlan', style='Accent.TButton', command=self.app.connect_telegram)
            self.acc_note.configure(text='Bağlanınca Telegram\'da sadece senin göreceğin gizli bir arşiv grubu açılır ve '
                                         'klipler oraya yedeklenmeye başlar.')
        elif cfg.get('hesap'):
            self._show_account(cfg['hesap'])

        box = ttk.LabelFrame(t, text=' Başlangıç ', padding=12)
        box.pack(fill='x', pady=(14, 0))
        ttk.Checkbutton(box, text='Windows açılınca otomatik başlat (yedekleme arka planda sürer)',
                        variable=self.app.autostart, command=self.app.toggle_autostart).pack(anchor='w')
        r = ttk.Frame(box)
        r.pack(fill='x', pady=(8, 0))
        ttk.Label(r, text=f'Ayarların yeri: {kk.DATA_DIR}', style='Muted.TLabel').pack(side='left')
        ttk.Button(r, text='Aç', command=self.app.menu_open_data).pack(side='left', padx=8)

        box = ttk.LabelFrame(t, text=' Güncelleme ', padding=12)
        box.pack(fill='x', pady=(14, 0))
        r = ttk.Frame(box)
        r.pack(fill='x')
        ttk.Label(r, text=f'Yüklü sürüm: {kk.VERSION}', style='H2.TLabel').pack(side='left')
        ttk.Button(r, text='Sürüm notları', command=lambda: webbrowser.open(
            f'https://github.com/{kk.GUNCELLEME_REPO}/releases')).pack(side='right')
        self.upd_btn = ttk.Button(r, text='Güncellemeleri kontrol et', style='Accent.TButton',
                                  command=self.check_updates)
        self.upd_btn.pack(side='right', padx=8)
        self.upd_msg = ttk.Label(box, text='', style='Muted.TLabel', wraplength=880, justify='left')
        self.upd_msg.pack(anchor='w', pady=(6, 0))
        ttk.Label(box, text='Program açılışta ve arka planda 6 saatte bir kendiliğinden de bakar; yeni sürüm varsa '
                            'kendisi kurar.', style='Muted.TLabel', wraplength=880).pack(anchor='w', pady=(2, 0))
        self.show_update_info()

    def _show_account(self, h):
        self.acc_label.configure(text=account_text(h))
        bits = []
        if h.get('tel_son'):
            bits.append(f'Telefon: •••• {h["tel_son"]}')
        bits.append('Premium: var (4 GB\'a kadar tek parça)' if h.get('premium')
                    else 'Premium: yok (2 GB üstü parçalı gider)')
        bits.append(f'Arşiv grubu: {kk.load_config().get("kanal_adi")}')
        self.acc_sub.configure(text='  ·  '.join(bits), foreground=GREY)

    def load_account(self):
        """Hesap bilgisini Telegram'dan tazeler (pencere başına bir kere)."""
        if self._acc_loaded or self._acc_busy or kk.load_config().get('telegramsiz'):
            return
        self._acc_busy = True
        if not kk.load_config().get('hesap'):
            self.acc_label.configure(text='Hesap bilgisi alınıyor…')
        self.app.async_.run(self._fetch_account(), self._account_fetched)

    @staticmethod
    async def _fetch_account():
        client = await kk.connect(kk.load_config())
        try:
            me = await client.get_me()
        finally:
            await client.disconnect()
        return kk.remember_account(kk.account_info(me))

    def _account_fetched(self, fut):
        self._acc_busy = False
        if not self.winfo_exists():  # bu arada çıkış yapılmış
            return
        try:
            h = fut.result()
        except Exception as e:
            if type(e).__name__ == 'NotLoggedIn':
                self.acc_label.configure(text='Telegram oturumu kapalı')
                self.acc_sub.configure(text='Durum sekmesindeki "Telegram girişini yenile" ile tekrar giriş yap.',
                                       foreground=ORANGE)
            elif not kk.load_config().get('hesap'):
                self.acc_label.configure(text='Hesap bilgisi alınamadı')
                self.acc_sub.configure(text=friendly_error(e), foreground=ORANGE)
            return
        self._acc_loaded = True
        self._show_account(h)

    def logout(self):
        if self.restore_future and not self.restore_future.done():
            messagebox.showinfo('Klip Kalkanı', 'Önce Geri Yükle sekmesindeki indirmeyi durdur.')
            return
        h = kk.load_config().get('hesap')
        who = f' ({account_text(h)})' if h else ''
        if not messagebox.askyesno('Klip Kalkanı', f'Telegram hesabından{who} çıkış yapılsın mı?\n\n'
                                   '• Yedekleme durur; tekrar giriş yapınca kaldığı yerden sürer.\n'
                                   '• Telegram\'daki arşiv grubu ve klipler silinmez.\n'
                                   '• Bu bilgisayar, Telegram\'daki cihaz listenden de çıkar.'):
            return
        self.logout_btn.configure(state='disabled')
        self.acc_sub.configure(text='Çıkış yapılıyor…', foreground=GREY)
        self.app.async_.run(kk.telegram_logout(prepare=kk.stop_backup), self._logged_out)

    def _logged_out(self, fut):
        try:
            fut.result()
        except Exception as e:
            self.logout_btn.configure(state='normal')
            self.acc_sub.configure(text=f'Çıkış yapılamadı: {friendly_error(e)}', foreground=RED)
            return
        self.app.show(LoginView)

    def check_updates(self):
        self.app.check_updates(manual=True)

    def show_update_info(self):
        """Son güncelleme kontrolünün sonucunu yazar (açılıştaki otomatik kontrol ya da düğme)."""
        busy = self.app.update_busy
        self.upd_btn.configure(state='disabled' if busy else 'normal')
        if busy:
            self.upd_msg.configure(text='GitHub\'a bakılıyor…', foreground=GREY)
            return
        if self.app.update_info is None:
            self.upd_msg.configure(text='')
            return
        text, color = update_result_text(self.app.update_info)
        self.upd_msg.configure(text=text, foreground=color)

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
        if kk.load_config().get('telegramsiz'):
            messagebox.showinfo('Klip Kalkanı', 'Geri yüklemek için önce Telegram\'a bağlanman lazım '
                                                '(Hesap > Telegram\'a bağlan).')
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


# ---------------------------------------------------------------- Gelişmiş ayarlar

def _int(text, lo, hi, label):
    try:
        v = int(str(text).strip())
    except ValueError:
        v = None
    if v is None or not lo <= v <= hi:
        raise ValueError(f'"{label}": {lo} ile {hi} arasında bir sayı yaz.')
    return v


def _hhmm(text, label):
    t = str(text).strip().replace('.', ':')
    try:
        h, m = (int(x) for x in t.split(':'))
    except ValueError:
        h = m = -1
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f'"{label}": saati SS:DD biçiminde yaz (örn. 01:00).')
    return f'{h:02d}:{m:02d}'


class AdvancedSettings(tk.Toplevel):
    """Gelişmiş ayarlar: ayarlar.json'daki her şey tek yerde. Kaydedince arka plan birkaç saniye içinde uygular.
    (Klip klasörleri Klasörler sekmesinde, Telegram hesabı Ayarlar sekmesinde.)"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('Gelişmiş ayarlar')
        self.transient(app)
        s = app.ui_scale
        self.geometry(f'{int(840 * s)}x{int(740 * s)}')
        self.minsize(int(640 * s), int(460 * s))
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass
        self.bgc = ttk.Style(self).lookup('TFrame', 'background') or '#1c1c1c'
        self.configure(bg=self.bgc)
        self.fields = {}    # anahtar → (okuyucu, yazıcı)
        bottom = ttk.Frame(self, padding=(16, 10))
        bottom.pack(side='bottom', fill='x')
        ttk.Button(bottom, text='Varsayılanlara dön', command=self.reset).pack(side='left')
        ttk.Button(bottom, text='Kaydet', style='Accent.TButton', command=self.save).pack(side='right')
        ttk.Button(bottom, text='Vazgeç', command=self.destroy).pack(side='right', padx=8)
        canvas = tk.Canvas(self, bg=self.bgc, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        self.body = ttk.Frame(canvas, padding=(18, 14))
        win = canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))
        self.bind('<MouseWheel>', lambda e: canvas.yview_scroll(int(-e.delta / 120), 'units'))
        self.bind('<Escape>', lambda _e: self.destroy())
        self._build(kk.load_config())
        self.focus_set()

    # alan yardımcıları
    def section(self, title, note=None):
        box = ttk.LabelFrame(self.body, text=f' {title} ', padding=12)
        box.pack(fill='x', pady=(0, 12))
        box.columnconfigure(1, weight=1)
        if note:
            ttk.Label(box, text=note, style='Muted.TLabel', wraplength=720, justify='left').grid(
                row=0, column=0, columnspan=3, sticky='w', pady=(0, 6))
        return box

    @staticmethod
    def _row(box, label, widget, hint=None):
        r = box.grid_size()[1]
        ttk.Label(box, text=label).grid(row=r, column=0, sticky='nw', pady=4, padx=(0, 12))
        widget.grid(row=r, column=1, sticky='w', pady=4)
        if hint:
            ttk.Label(box, text=hint, style='Muted.TLabel', wraplength=560, justify='left').grid(
                row=r + 1, column=1, sticky='w', pady=(0, 6))

    def check(self, box, key, label, value, hint=None):
        var = tk.BooleanVar(value=bool(value))
        r = box.grid_size()[1]
        ttk.Checkbutton(box, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky='w', pady=3)
        if hint:
            ttk.Label(box, text=hint, style='Muted.TLabel', wraplength=640, justify='left').grid(
                row=r + 1, column=0, columnspan=2, sticky='w', padx=(28, 0), pady=(0, 6))
        self.fields[key] = (var.get, var.set)

    def number(self, box, key, label, value, lo, hi, hint=None):
        var = tk.StringVar(value=str(value))
        self._row(box, label, ttk.Spinbox(box, from_=lo, to=hi, textvariable=var, width=8), hint)
        self.fields[key] = (lambda: _int(var.get(), lo, hi, label), lambda v: var.set(str(v)))

    def text(self, box, key, label, value, hint=None, width=36):
        var = tk.StringVar(value=value or '')
        self._row(box, label, ttk.Entry(box, textvariable=var, width=width), hint)
        self.fields[key] = (lambda: var.get().strip(), lambda v: var.set(v or ''))

    def lines(self, box, key, label, value, hint=None, height=4, as_dict=False):
        frame = tk.Frame(box, bg='#3a3a3a', padx=1, pady=1)
        t = tk.Text(frame, height=height, width=58, bg='#262626', fg=MENU_FG, insertbackground=MENU_FG,
                    relief='flat', font=('Consolas', 10), wrap='none', undo=True)
        t.pack(fill='both', expand=True)
        self._row(box, label, frame, hint)

        def fmt(v):
            if as_dict:
                return '\n'.join(f'{k} = {x}' for k, x in (v or {}).items())
            return '\n'.join(v or [])

        def get():
            rows = [x.strip() for x in t.get('1.0', 'end').splitlines() if x.strip()]
            if not as_dict:
                return rows
            out = {}
            for x in rows:
                if '=' not in x:
                    raise ValueError(f'"{label}": her satır "ad = değer" biçiminde olmalı ("{x}").')
                k, v = x.split('=', 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
            return out

        def put(v):
            t.delete('1.0', 'end')
            t.insert('1.0', fmt(v))
        put(value)
        self.fields[key] = (get, put)

    def _build(self, cfg):
        ttk.Label(self.body, text='Gelişmiş ayarlar', style='Title.TLabel').pack(anchor='w')
        ttk.Label(self.body, text='Buradaki her şey ayarlar dosyasında durur. Kaydedince arka plan birkaç saniye içinde '
                                  'yeni ayarlarla devam eder. Klip klasörleri Klasörler sekmesinde, Telegram hesabı '
                                  'Ayarlar sekmesinde.', style='Muted.TLabel', wraplength=740).pack(anchor='w',
                                                                                                    pady=(2, 14))
        # yükleme hızı: [gece, gündüz] saatleri ve limitleri
        box = self.section('Yükleme hızı', 'İki zaman dilimi: gece yüksek, gündüz düşük tutulabilir. Saatler 24 saat '
                                           'biçiminde; bitiş başlangıçtan küçükse gece yarısını geçer.')
        (n0, n1), (d0, d1) = plan_times(cfg)
        day, night = plan_values(cfg)
        self.plan = {}
        for key, label, t0, t1, mbit in (('gece', 'Gece', n0, n1, night), ('gunduz', 'Gündüz', d0, d1, day)):
            r = ttk.Frame(box)
            a, b = tk.StringVar(value=t0), tk.StringVar(value=t1)
            ttk.Entry(r, textvariable=a, width=6).pack(side='left')
            ttk.Label(r, text=' – ').pack(side='left')
            ttk.Entry(r, textvariable=b, width=6).pack(side='left')
            cb = ttk.Combobox(r, values=SPEED_CHOICES, width=12)
            cb.set(mbit_to_choice(mbit))
            cb.pack(side='left', padx=(16, 6))
            ttk.Label(r, text='Mbit').pack(side='left')
            self._row(box, label + ':', r)
            self.plan[key] = (label, a, b, cb)
        mx = ttk.Combobox(box, values=SPEED_CHOICES, width=12)
        mx.set(mbit_to_choice(cfg.get('max_hiz_mbit') or UNLIMITED))
        self._row(box, 'Plan dışı saatler:', mx, 'Plana girmeyen saatlerde en fazla bu hız (Mbit).')
        self.fields['max_hiz_mbit'] = (lambda: self._mbit(mx.get(), 'Plan dışı saatler'),
                                       lambda v: mx.set(mbit_to_choice(v or UNLIMITED)))
        self.number(box, 'paralel_parca', 'Aynı anda giden parça:', cfg.get('paralel_parca', 8), 1, 32,
                    'Genelde 8 yeter; çok artırmak hızı artırmaz, bağlantıyı zorlar.')

        box = self.section('Oyun algılama')
        self.check(box, 'oyunda_dur', 'Oyun açıkken yüklemeyi durdur (ping bozulmasın)', cfg.get('oyunda_dur', True))
        self.check(box, 'tam_ekranda_dur', 'Tam ekran bir uygulama açıkken de durdur', cfg.get('tam_ekranda_dur', True))
        self.number(box, 'oyun_kapaninca_bekle_sn', 'Oyun kapanınca bekle (sn):', cfg.get('oyun_kapaninca_bekle_sn', 60),
                    0, 3600)
        self.lines(box, 'oyun_exe', 'Oyun programları:', cfg.get('oyun_exe'),
                   'Her satıra bir .exe adı. Unreal oyunları (…-Win64-Shipping.exe) zaten tanınır.', height=6)

        box = self.section('Tarama ve sıra')
        self.number(box, 'tarama_dakika', 'Klasörleri tarama (dk):', cfg.get('tarama_dakika', 10), 1, 1440,
                    'Yeni klipler en geç bu kadar dakikada fark edilir.')
        self.number(box, 'yeni_klip_gun', 'Öne geçen yeni klipler (gün):', cfg.get('yeni_klip_gun', 3), 0, 365,
                    'Son bu kadar günün klipleri eski arşivden önce yüklenir.')
        self.text(box, 'uzantilar', 'Video uzantıları:', ', '.join(cfg.get('uzantilar') or []),
                  'Virgülle ayır (örn. .mp4, .mkv, .mov).')
        self.lines(box, 'genel_klasorler', 'Oyun adı olmayan klasörler:', cfg.get('genel_klasorler'),
                   'Her satıra bir klasör adı (ör. yedek klasörlerin); oyun adı bunların altındaki klasörden okunur.')
        self.lines(box, 'oyun_takma_adlari', 'Oyun adı düzeltmeleri:', cfg.get('oyun_takma_adlari'),
                   'Her satıra "klasör adı = oyun adı" (ör. buradavalorant = Valorant).', as_dict=True)

        box = self.section('Telegram')
        self.check(box, 'oyun_konulari', 'Her oyuna arşiv grubunda ayrı konu (topic) aç', cfg.get('oyun_konulari', True))
        self.text(box, 'kanal_adi', 'Yeni arşiv grubunun adı:', cfg.get('kanal_adi'),
                  'Sadece yeni bir grup açılırsa kullanılır; var olan grubun adı Telegram\'dan değişir.', width=30)

        box = self.section('Düzenleyici')
        self.lines(box, 'ses_adlari', 'Ses kanalı adları:', cfg.get('ses_adlari'),
                   'Her satıra "orijinal ad = senin verdiğin ad" (ör. r5apex_dx12 = Apex). Düzenle sekmesinde ✎ ile de '
                   'değişir.', as_dict=True)

        box = self.section('Diğer')
        self.check(box, 'uyku_engelle', 'Yükleme sürerken bilgisayar uykuya geçmesin (prizdeyken)',
                   cfg.get('uyku_engelle', True))
        self.check(box, 'otomatik_guncelle', 'Yeni sürümleri kendiliğinden kur', cfg.get('otomatik_guncelle', True),
                   'Kapalıyken Yardım > Güncellemeleri kontrol et ile elle kurarsın.')

        box = self.section('Bilgi')
        mode = 'Telegram\'sız (yedekleme kapalı)' if cfg.get('telegramsiz') else 'Telegram\'a yedekleme'
        for label, value, path in (('Sürüm:', kk.VERSION, None), ('Kullanım:', mode, None),
                                   ('Program klasörü:', BASE, BASE), ('Ayar klasörü:', kk.DATA_DIR, kk.DATA_DIR)):
            r = ttk.Frame(box)
            ttk.Label(r, text=value, style='Muted.TLabel').pack(side='left')
            if path:
                ttk.Button(r, text='Aç', command=lambda p=path: os.startfile(p)).pack(side='left', padx=8)
            self._row(box, label, r)

    @staticmethod
    def _mbit(text, label):
        v = choice_to_mbit(text)
        if v is None:
            raise ValueError(f'"{label}": listeden seç ya da Mbit olarak sayı yaz.')
        return v

    def collect(self):
        out = {key: get() for key, (get, _) in self.fields.items()}
        out['uzantilar'] = [('.' + x.lstrip('.')).lower() for x in
                            out['uzantilar'].replace(';', ',').replace(' ', ',').split(',') if x.strip('. ')]
        if not out['uzantilar']:
            raise ValueError('"Video uzantıları": en az bir uzantı yaz.')
        out['kanal_adi'] = out['kanal_adi'] or kk.DEFAULT_CONFIG['kanal_adi']
        plan = []
        for key in ('gece', 'gunduz'):
            label, a, b, cb = self.plan[key]
            plan.append({'baslangic': _hhmm(a.get(), label + ' başlangıcı'), 'bitis': _hhmm(b.get(), label + ' bitişi'),
                         'mbit': self._mbit(cb.get(), label)})
        out['hiz_plani'] = plan
        return out

    def reset(self):
        d = kk.DEFAULT_CONFIG
        for key, (_, put) in self.fields.items():
            if key == 'uzantilar':
                put(', '.join(d['uzantilar']))
            elif key in d:
                put(d[key])
        for key, p in zip(('gece', 'gunduz'), d['hiz_plani']):
            _, a, b, cb = self.plan[key]
            a.set(p['baslangic'])
            b.set(p['bitis'])
            cb.set(mbit_to_choice(p['mbit']))

    def save(self):
        try:
            changes = self.collect()
        except ValueError as e:
            messagebox.showerror('Gelişmiş ayarlar', str(e), parent=self)
            return
        update_config(**changes)
        if isinstance(self.app.view, DashboardView):
            self.app.view.refresh_settings()
        self.app.say('✔ Gelişmiş ayarlar kaydedildi; arka plan birkaç saniye içinde yeni ayarlarla devam eder.', GREEN)
        self.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # yüksek çözünürlükte bulanık olmasın
    except Exception:
        pass
    try:  # 1.7 öncesi kurulum: ayarlar, giriş ve kayıtlar program klasöründen %APPDATA%\KlipKalkani'ye
        kk.migrate_data(stop_running=True)
    except Exception as e:
        print('Veriler taşınamadı, eski yerden devam:', e)
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
