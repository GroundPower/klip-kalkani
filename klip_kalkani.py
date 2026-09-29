# -*- coding: utf-8 -*-
"""Klip Kalkanı: oyun kliplerini otomatik olarak özel bir Telegram kanalına yedekler.

Komutlar (.venv\\Scripts\\python.exe klip_kalkani.py <komut>):
  giris                 Telegram'a giriş yap, arşiv kanalını hazırla (bir kere)
  tara                  Klasörleri tara, ne kadarının yedeklendiğini göster
  calis [--limit N]     Sürekli çalış: yeni klipleri bulup yükler (arka plan görevi bunu çalıştırır)
  durum                 İlerlemeyi göster
  dogrula               Kanaldaki yedekleri kontrol et; eksik çıkan tekrar yüklenir
  geri-yukle [HEDEF]    Klipleri kanaldan indirip HEDEF klasöre koy (--oyun, --ay, --ara, --hepsi, --liste)
  indeks-yenile         Yerel veritabanını kanaldan yeniden kur (bilgisayar değişirse)
  yukle-dene DOSYA      Tek bir dosyayı hemen yükle (deneme için)
  cikis                 Telegram'dan çıkış yap (yedekleme durur; Telegram'daki yedekler kalır)
  kaldir                Otomatik başlatmayı kaldır

Hiçbir komut bilgisayardaki klipleri silmez, taşımaz veya değiştirmez. Kanala sadece ekleme yapılır.

Ayarlar, Telegram girişi, veritabanı ve günlükler %APPDATA%\\KlipKalkani'de durur; program başka bir klasöre
açılsa ya da güncellense de kaybolmaz. (1.7'den önce program klasöründeydi; ilk açılışta oraya taşınır.)
"""

import argparse
import asyncio
import base64
import collections
import ctypes
import datetime as dt
import fnmatch
import getpass
import glob
import hashlib
import html
import io
import json
import logging
import logging.handlers
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import psutil

VERSION = '1.9'
BASE = os.path.dirname(os.path.abspath(__file__))  # program dosyaları
APPDATA_DIR = os.path.join(os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming'),
                           'KlipKalkani')
# program klasöründen %APPDATA%'ya taşınan kullanıcı verisi (veritabanı, log ve ayarlar.json ayrıca taşınır)
DATA_PATTERNS = ('klip_kalkani*.session*', 'klip_kalkani.*.db', 'durum.json')


def set_data_dir(d):
    """Kullanıcı verisinin (ayarlar, giriş, veritabanı, durum, günlük) durduğu klasör."""
    global DATA_DIR, CONFIG_PATH, DB_PATH, SESSION_BASE, SESSION_FILE, LOGIN_SESSION_BASE, STATUS_PATH, LOG_DIR
    global SCAN_REQUEST
    DATA_DIR = d
    CONFIG_PATH = os.path.join(d, 'ayarlar.json')
    DB_PATH = os.path.join(d, 'klip_kalkani.db')
    SESSION_BASE = os.path.join(d, 'klip_kalkani')  # Telethon sonuna .session ekler
    SESSION_FILE = SESSION_BASE + '.session'
    LOGIN_SESSION_BASE = SESSION_BASE + '-giris'  # pencereden giriş sürerken; bitince SESSION_FILE olur
    STATUS_PATH = os.path.join(d, 'durum.json')
    LOG_DIR = os.path.join(d, 'log')
    SCAN_REQUEST = os.path.join(d, 'tara.istek')  # pencere "şimdi tara" deyince yükleyici hemen tarar


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# Veri %APPDATA%'da; henüz taşınmamış eski bir kurulumun verisi program klasöründeyse (taşınana kadar) oradan.
set_data_dir(BASE if (not os.path.exists(os.path.join(APPDATA_DIR, 'ayarlar.json'))
                      and os.path.exists(os.path.join(BASE, 'ayarlar.json'))) else APPDATA_DIR)

PART_SIZE = 512 * 1024                 # Telegram'ın kabul ettiği en büyük parça
SMALL_FILE = 10 * 1024 * 1024          # bunun altı saveFilePart ile gider
MAX_PARTS = {False: 3990, True: 7990}  # sınır 4000 parça (Premium: 8000) = ~2 GB (4 GB)
READ_BLOCK = 8 * 1024 * 1024
FP_SAMPLE = 256 * 1024
STABLE_SECONDS = 120                   # yeni yazılan dosyaya bu kadar dokunma
META_TAG = 'kk1'
NVIDIA_PATTERN = '* 20??.??.?? - *'
STREAMABLE_EXTS = {'.mp4', '.mov', '.m4v'}  # Telegram'da video olarak oynatılabilenler
TASK_NAME = 'Klip Kalkani'

log = logging.getLogger('klip_kalkani')


def _src(yol, oncelik, **extra):
    d = {'yol': yol, 'oncelik': oncelik}
    d.update(extra)
    return d


DEFAULT_CONFIG = {
    'api_id': None,
    'api_hash': None,
    'kanal_id': None,
    'kanal_hash': None,
    'kanal_turu': None,           # 'forum' = her oyuna ayrı konu (topic) açılan gizli grup
    'kanal_adi': 'Klip Arşivi 🎮',
    'oyun_konulari': True,
    'uzantilar': ['.mp4', '.mkv', '.mov', '.webm', '.avi'],
    # Klip klasörleri; kurulumda (ya da arayüzden) doldurulur ve ayarlar.json'da durur.
    # Sıra: önce yedekleme açıldıktan sonra çekilen klipler, sonra küçük 'oncelik' önce, aynı öncelikte yeni olan önce.
    # desen: dosya adı kalıbı, haric: girilmeyecek klasör adları, alt_klasorler: false = sadece o klasör.
    'kaynaklar': [],
    'yeni_klip_gun': 3,
    'tarama_dakika': 10,
    # Saat planı: o saatlerde en fazla kaç Mbit ile yüklensin (0 = o saatlerde hiç yükleme).
    'hiz_plani': [
        {'baslangic': '01:00', 'bitis': '09:00', 'mbit': 20},
        {'baslangic': '09:00', 'bitis': '01:00', 'mbit': 8},
    ],
    'max_hiz_mbit': 8,            # plana uymayan saatler için (0 = sınırsız)
    'duraklat': False,            # arayüzdeki Duraklat tuşu
    'paralel_parca': 8,
    'oyunda_dur': True,           # aşağıdaki oyunlardan biri (veya herhangi bir Unreal oyunu) açıkken yükleme durur
    'tam_ekranda_dur': True,      # özel tam ekran (exclusive fullscreen) bir oyun varken de durur
    'oyun_kapaninca_bekle_sn': 60,
    'uyku_engelle': True,         # yükleme sürerken bilgisayar uykuya geçmesin (prizdeyken)
    'oyun_exe': [
        'League of Legends.exe', 'VALORANT.exe', 'r5apex.exe', 'r5apex_dx12.exe', 'RainbowSix.exe',
        'RainbowSix_Vulkan.exe', 'RainbowSix_DX12.exe', 'Overwatch.exe', 'cs2.exe', 'csgo.exe', 'GTA5.exe',
        'GTA5_Enhanced.exe', 'SoTGame.exe', 'bf1.exe', 'bf2042.exe', 'bf6.exe', 'DyingLightGame.exe',
        'DyingLightGame_x64_rwdi.exe', 'hl2.exe', 'gmod.exe', 'PartyAnimals.exe', 'Crab Game.exe',
        'RocketLeague.exe', 'eldenring.exe', 'dota2.exe', 'TslGame.exe', 'RobloxPlayerBeta.exe',
        'Minecraft.Windows.exe', 'destiny2.exe', 'RustClient.exe', 'EscapeFromTarkov.exe', 'helldivers2.exe',
        'cod.exe', 'HuntGame.exe', 'Warframe.x64.exe', 'Phasmophobia.exe', 'Lethal Company.exe',
    ],
    # Oyun adı sanılmaması gereken ek klasör adları (ör. kendi yedek klasörlerin) ve ek oyun adı düzeltmeleri
    'genel_klasorler': [],
    'oyun_takma_adlari': {},
    'telegramsiz': False,         # Telegram'a bağlanmadan kullanım: düzenleyici ve özetler çalışır, yedekleme kapalı
    'otomatik_guncelle': True,    # yeni sürümler kendiliğinden kurulsun
    'ses_adlari': {},             # düzenleyicide ses kanalı adları: {orijinal ad: senin verdiğin ad}
}

# Oyun adı olmayan, sadece kapsayıcı klasör adları (oyun adı bu klasörlerin altından okunur)
GENERIC_DIRS = {
    'clips', 'medal', 'outplayed', 'nvidia', 'videos', 'yedek', 'editor', 'screenshots', 'reels', 'overwolf',
    'temp-capture', 'desktop', 'users', 'captures', 'radeon relive', 'videolar',
    (os.environ.get('USERNAME') or '').lower(),
}
# Aynı oyunun farklı yazılışlarını tek konuda toplamak için (anahtarlar norm_key biçiminde)
GAME_ALIASES = {
    'polariswin64shipping': 'TEKKEN 8',
    'editoractivedrafts': 'Medal Editör',
    'editorimported': 'Medal Editör',
    'temp': 'Medal Editör',
    'desktop': 'Masaüstü Kayıtları',
    'rainbow6siege': 'Rainbow Six Siege',
    'rainbowsix': 'Rainbow Six Siege',
    'tomclancysrainbowsixsiege': 'Rainbow Six Siege',
    'csgo': 'CS:GO',
    'halflife': 'Half-Life',
    'amongus': 'Among Us',
    'counterstrike16': 'Counter-Strike 1.6',
    'obs': 'OBS Kayıtları',
    'overwatch': 'Overwatch 2',
}


def naming_rules(cfg):
    """Koddaki genel kurallar + kullanıcının ayarlarındaki ekler."""
    generic = GENERIC_DIRS | {d.lower() for d in cfg.get('genel_klasorler') or []}
    aliases = dict(GAME_ALIASES)
    aliases.update({norm_key(k): v for k, v in (cfg.get('oyun_takma_adlari') or {}).items()})
    return generic, aliases
SKIP_DIRS = {'$recycle.bin', 'system volume information'}

RE_MEDAL = re.compile(r'MedalTV([A-Za-z0-9]+)', re.I)
RE_NVIDIA = re.compile(r'^(.+?) (\d{4})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})')
RE_OUTPLAYED = re.compile(r'^(.+?)[ _](\d{2})-(\d{2})-(\d{4})_(\d{1,2})-(\d{1,2})-(\d{1,2})')
RE_STAMP = re.compile(r'(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})')

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    fp TEXT,
    source TEXT,
    prio INTEGER,
    game TEXT,
    taken REAL,
    seen REAL
);
CREATE INDEX IF NOT EXISTS files_fp ON files(fp);
CREATE TABLE IF NOT EXISTS blobs (
    fp TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    nparts INTEGER,
    chunk_len INTEGER,
    sha256 TEXT,
    done_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS parts (
    fp TEXT NOT NULL,
    idx INTEGER NOT NULL,
    nparts INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    uploaded_at REAL NOT NULL,
    PRIMARY KEY (fp, idx)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS topics (
    game TEXT PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

README = (
    '🛡 <b>Klip Kalkanı arşivi</b>\n\n'
    'Bu grup oyun kliplerinin otomatik yedeği. Her oyunun kendi konusu var. <b>Mesajları silme.</b>\n\n'
    '• Her klip orijinal dosya olarak duruyor, sıkıştırma yok.\n'
    "• 2 GB'tan büyük dosyalar .001, .002… diye parçalı. Parçaları aynı klasöre indirip 7-Zip ile .001'i aç "
    'ya da şunu çalıştır:\n<code>copy /b "klip.mp4.001" + "klip.mp4.002" "klip.mp4"</code>\n'
    '• Oyuna göre bulmak için #LeagueofLegends gibi etiketlere dokun.\n'
    '• Toplu geri yükleme: Klip Kalkanı penceresindeki "Geri Yükle" sekmesi, ya da bu dosyadaki programın '
    '<code>geri-yukle</code> komutu.'
)
ABOUT = 'Klip Kalkanı: oyun kliplerinin otomatik yedeği. Mesajları silme!'


class NotLoggedIn(Exception):
    pass


class FileChanged(Exception):
    pass


# ---------------------------------------------------------------- yardımcılar

DEFAULTS_PATH = os.path.join(BASE, 'varsayilan.json')  # paketle gelen (API bilgileri); güncellemede ezilir


def load_config():
    """Öncelik: ayarlar.json (kullanıcının) > varsayilan.json (paketin) > koddaki varsayılanlar."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in (DEFAULTS_PATH, CONFIG_PATH):
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    write_json_atomic(CONFIG_PATH, cfg, indent=2)


def write_json_atomic(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    return db


def meta_get(db, key, default=None):
    row = db.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)', (key, json.dumps(value, ensure_ascii=False)))
    db.commit()


def fmt_size(n):
    for unit, div in (('TB', 1e12), ('GB', 1e9), ('MB', 1e6), ('KB', 1e3)):
        if n >= div:
            return f'{n / div:.2f} {unit}'.replace('.', ',')
    return f'{n} B'


def fmt_count(n):
    return f'{n:,}'.replace(',', '.')


def fmt_duration(sec):
    sec = max(0, int(sec))
    if sec < 90:
        return f'{sec} sn'
    if sec < 5400:
        return f'{sec // 60} dk'
    if sec < 172800:
        return f'{sec / 3600:.1f} saat'.replace('.', ',')
    return f'{sec / 86400:.1f} gün'.replace('.', ',')


def as_list(x):
    if not x:
        return []
    return [x] if isinstance(x, str) else list(x)


def norm_key(s):
    return re.sub(r'[\W_]+', '', s.lower())


def hashtag(game):
    tag = re.sub(r'[\W_]+', '', game)
    return '#' + (tag or 'klip')


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s).strip(' .') or 'Diğer'


def _ts(y, mo, d, h=0, mi=0, s=0):
    try:
        return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_medal_name(name):
    """'MedalTVTEKKEN820240529223125.mp4' -> ('TEKKEN8', zaman). Oyun adı rakamla bitebildiği için
    zaman damgası sondaki rakamların son 14'ü (ya da milisaniyeli ise son 17'nin ilk 14'ü) olarak aranır."""
    m = RE_MEDAL.search(name)
    if not m:
        return None
    s = m.group(1)
    digits = re.search(r'\d*$', s).group(0)
    if len(digits) < 14:
        return None
    head = s[:len(s) - len(digits)]
    for cut in (17, 14):
        if len(digits) >= cut:
            stamp = digits[len(digits) - cut:len(digits) - cut + 14]
            t = _ts(stamp[0:4], stamp[4:6], stamp[6:8], stamp[8:10], stamp[10:12], stamp[12:14])
            if t and stamp.startswith('20'):
                return head + digits[:len(digits) - cut], t
    return None


def detect_game_date(path, mtime, pretty, rules=None):
    """Klibin oyununu ve çekildiği zamanı dosya/klasör adından çıkar. rules: naming_rules(cfg)"""
    generic, aliases = rules or (GENERIC_DIRS, GAME_ALIASES)
    name = os.path.basename(path)
    folder_game = None
    for d in reversed(path.split(os.sep)[1:-1]):
        m = RE_OUTPLAYED.match(d)
        if m:
            folder_game = m.group(1)
            break
        if d.lower() in generic:
            continue
        folder_game = 'Discord Arşivi' if d.lower().endswith('.html_files') else d
        break
    game = taken = None
    medal = parse_medal_name(name)
    if medal:
        compact, taken = medal
        if norm_key(compact) in pretty:
            game = pretty[norm_key(compact)]
        elif folder_game and norm_key(folder_game) == norm_key(compact):
            game = folder_game
        else:
            game = compact or folder_game
    elif (m := RE_NVIDIA.match(name)):
        game = folder_game or m.group(1)
        taken = _ts(*m.groups()[1:])
    elif (m := RE_OUTPLAYED.match(name)):
        game = m.group(1)
        taken = _ts(m.group(4), m.group(2), m.group(3), m.group(5), m.group(6), m.group(7))
    elif (m := RE_STAMP.search(name)):
        taken = _ts(*m.groups())
    game = re.sub(r'\s+', ' ', game or folder_game or 'Diğer').strip()
    game = aliases.get(norm_key(game), game)
    return game, taken or mtime


def fingerprint(path, size):
    """Boyut + baş/orta/son parçaların özeti: aynı klibin farklı kopyalarını tanımak için."""
    h = hashlib.sha256(str(size).encode())
    with open(path, 'rb') as f:
        if size <= 3 * FP_SAMPLE:
            h.update(f.read())
        else:
            h.update(f.read(FP_SAMPLE))
            f.seek(size // 2)
            h.update(f.read(FP_SAMPLE))
            f.seek(size - FP_SAMPLE)
            h.update(f.read(FP_SAMPLE))
    return h.hexdigest()[:32]


def probe_video(path):
    """Klipten süre, çözünürlük ve küçük bir önizleme resmi (JPEG) çıkarır. Okunamazsa None."""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(path) as c:
            vs = next((s for s in c.streams if s.type == 'video'), None)
            if vs is None:
                return None
            if c.duration:
                dur = c.duration / av.time_base
            elif vs.duration and vs.time_base:
                dur = float(vs.duration * vs.time_base)
            else:
                dur = 0.0
            thumb = None
            try:
                if dur > 4 and vs.time_base:
                    c.seek(int(dur * 0.2 / vs.time_base), stream=vs)  # klibin %20'sinden bir kare
                for frame in c.decode(vs):
                    img = frame.to_image().convert('RGB')
                    img.thumbnail((320, 320))
                    for q in (85, 70, 55, 40):
                        buf = io.BytesIO()
                        img.save(buf, 'JPEG', quality=q)
                        if buf.tell() <= 190_000:
                            thumb = buf.getvalue()
                            break
                    break
            except Exception as e:
                log.info('Önizleme resmi çıkarılamadı: %s (%s)', os.path.basename(path), e)
            return {'duration': float(dur), 'w': int(vs.codec_context.width or 0),
                    'h': int(vs.codec_context.height or 0), 'thumb': thumb}
    except Exception as e:
        log.info('Video bilgisi okunamadı: %s (%s)', os.path.basename(path), e)
        return None


def detect_game(names, cfg):
    if cfg.get('oyunda_dur', True):
        for p in psutil.process_iter(['name']):
            n = (p.info.get('name') or '').lower()
            if n and (n in names or n.endswith('-win64-shipping.exe')):
                return f'oyun açık ({p.info["name"]})'
    if cfg.get('tam_ekranda_dur', True):
        state = ctypes.c_int(0)
        # 3 = QUNS_RUNNING_D3D_FULL_SCREEN. 2'ye (BUSY) bakmıyoruz: Discord açıkken bile 2 dönebiliyor.
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value == 3:
            return 'tam ekran oyun açık'
    return None


_awake = False


def keep_awake(on, cfg=None):
    global _awake
    if cfg is not None and not cfg.get('uyku_engelle', True):
        on = False
    if on == _awake:
        return
    if on:
        bat = psutil.sensors_battery()
        if bat is not None and not bat.power_plugged:
            return
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    else:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    _awake = on


def toast(title, text):
    def q(s):
        return "'" + s.replace("'", "''") + "'"
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
        "$n = $t.GetElementsByTagName('text')\n"
        f"$n.Item(0).AppendChild($t.CreateTextNode({q(title)})) | Out-Null\n"
        f"$n.Item(1).AppendChild($t.CreateTextNode({q(text)})) | Out-Null\n"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))\n"
    )
    enc = base64.b64encode(script.encode('utf-16-le')).decode()
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-EncodedCommand', enc], creationflags=0x08000000, timeout=60, capture_output=True)


def alert(db, key, title, text, every_hours=12):
    last = meta_get(db, 'uyari_' + key, 0)
    if time.time() - last < every_hours * 3600:
        return
    meta_set(db, 'uyari_' + key, time.time())
    log.warning('UYARI: %s: %s', title, text)
    try:
        toast(title, text)
    except Exception as e:  # bildirim gösterilemese de iş devam etsin
        log.warning('Bildirim gösterilemedi: %s', e)


_mutex = None
_mutex_ok = None


def single_instance():
    """Arka plan yükleyicisinden tek kopya çalışsın. Aynı süreçte tekrar çağrılınca ilk sonucu döndürür."""
    global _mutex, _mutex_ok
    if _mutex_ok is None:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        _mutex = k32.CreateMutexW(None, False, 'Local\\KlipKalkaniCalis')
        _mutex_ok = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    return _mutex_ok


def setup_logging(filename, console):
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, filename), maxBytes=5_000_000,
                                              backupCount=5, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%Y-%m-%d %H:%M:%S'))
    root.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%H:%M:%S'))
        root.addHandler(sh)
    logging.getLogger('telethon').setLevel(logging.WARNING)


# ---------------------------------------------------------------- tarama

def iter_source(src, exts):
    root = src['yol']
    patterns = [p.lower() for p in as_list(src.get('desen'))]
    excluded = {d.lower() for d in as_list(src.get('haric'))} | SKIP_DIRS

    def wanted(name):
        low = name.lower()
        if os.path.splitext(low)[1] not in exts:
            return False
        return not patterns or any(fnmatch.fnmatch(low, p) for p in patterns)

    if src.get('alt_klasorler', True):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d.lower() not in excluded]
            for fn in fns:
                if wanted(fn):
                    yield os.path.join(dp, fn)
    else:
        with os.scandir(root) as it:
            for e in it:
                if wanted(e.name) and e.is_file():
                    yield e.path


def scan(cfg):
    """Kaynak klasörleri tarar, veritabanını günceller. Hiçbir dosyaya yazmaz."""
    db = open_db()
    try:
        start = time.time()
        exts = {e.lower() for e in cfg['uzantilar']}
        found = {}
        unavailable = []
        for src in cfg['kaynaklar']:
            root = src['yol']
            if not os.path.isdir(root):
                unavailable.append(root)
                continue
            try:
                for p in iter_source(src, exts):
                    p = os.path.normpath(p)
                    prev = found.get(p)
                    if prev is None or src['oncelik'] < prev[1]:
                        found[p] = (root, src['oncelik'])
            except OSError as e:
                log.warning('Taranamadı: %s (%s)', root, e)
                unavailable.append(root)
        rules = naming_rules(cfg)
        pretty = {}
        for p in found:
            for d in p.split(os.sep)[1:-1]:
                if d.lower() not in rules[0]:
                    pretty.setdefault(norm_key(d), d)
        known = {r[0]: (r[1], r[2]) for r in db.execute('SELECT path, size, mtime FROM files')}
        new = changed = 0
        with db:
            for p, (root, prio) in found.items():
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                k = known.get(p)
                game, taken = detect_game_date(p, st.st_mtime, pretty, rules)
                if k is None:
                    db.execute('INSERT OR IGNORE INTO files(path, size, mtime, fp, source, prio, game, taken, seen) '
                               'VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)',
                               (p, st.st_size, st.st_mtime, root, prio, game, taken, start))
                    new += 1
                elif k[0] != st.st_size or k[1] != st.st_mtime:
                    db.execute('UPDATE files SET size=?, mtime=?, fp=NULL, source=?, prio=?, game=?, taken=?, seen=? '
                               'WHERE path=?', (st.st_size, st.st_mtime, root, prio, game, taken, start, p))
                    changed += 1
                else:
                    db.execute('UPDATE files SET source=?, prio=?, game=?, taken=?, seen=? WHERE path=?',
                               (root, prio, game, taken, start, p))
        meta_set(db, 'son_tarama', start)
        meta_set(db, 'erisilemeyen', unavailable)
        return {'start': start, 'found': len(found), 'new': new, 'changed': changed, 'unavailable': unavailable}
    finally:
        db.close()


def pending_rows(db, since):
    return db.execute(
        'SELECT f.path, f.size, f.mtime, f.fp, f.prio, f.game, f.taken, f.source, '
        '       b.status, b.next_try, b.attempts, b.last_error '
        'FROM files f LEFT JOIN blobs b ON b.fp = f.fp '
        "WHERE f.seen >= ? AND f.size > 0 AND (b.status IS NULL OR b.status != 'done')", (since,)).fetchall()


def build_queue(db, cfg, since):
    now = time.time()
    # Sadece yedekleme açıldıktan sonra kaydedilen klipler öne geçer; eskiler kaynak sırasıyla gider.
    fresh = max(now - cfg['yeni_klip_gun'] * 86400, meta_get(db, 'kurulum') or now)
    big = MAX_PARTS[False] * PART_SIZE
    best = {}
    items = []
    for r in pending_rows(db, since):
        if r['next_try'] and r['next_try'] > now:
            continue
        # aynı öncelikte: önce normal klipler (yeniden eskiye), 2 GB üstü dev kayıtlar en sona
        key = (0 if r['mtime'] >= fresh else r['prio'], r['size'] > big, -(r['taken'] or 0), r['path'])
        if r['fp']:
            cur = best.get(r['fp'])
            if cur is None or key < cur[0]:
                best[r['fp']] = (key, r)
        else:
            items.append((key, r))
    items.extend(best.values())
    items.sort(key=lambda x: x[0])
    return [r for _, r in items]


# ---------------------------------------------------------------- Telegram

def new_client(session, cfg, **kw):
    from telethon import TelegramClient
    os.makedirs(DATA_DIR, exist_ok=True)
    return TelegramClient(session, int(cfg['api_id']), cfg['api_hash'], device_model='Klip Kalkani',
                          system_version='Windows', app_version=VERSION, lang_code='tr', system_lang_code='tr',
                          receive_updates=False, **kw)


def make_client(cfg, forever=False):
    from telethon.sessions import SQLiteSession, StringSession
    if not cfg.get('api_id') or not cfg.get('api_hash'):
        raise NotLoggedIn('api_id/api_hash yok')
    if not os.path.exists(SESSION_FILE):
        raise NotLoggedIn('oturum dosyası yok')
    # Oturum dosyasını kilitlememek için anahtarı belleğe kopyalayıp onunla bağlanıyoruz.
    s = SQLiteSession(SESSION_BASE)
    try:
        string = StringSession.save(s)
    finally:
        s.close()
    if not string:
        raise NotLoggedIn('oturum boş')
    return new_client(StringSession(string), cfg, connection_retries=-1 if forever else 5, retry_delay=5,
                      request_retries=5, flood_sleep_threshold=300)


def channel_peer(cfg):
    from telethon import types
    if not cfg.get('kanal_id') or not cfg.get('kanal_hash'):
        raise NotLoggedIn('arşiv kanalı ayarlı değil')
    return types.InputPeerChannel(int(cfg['kanal_id']), int(cfg['kanal_hash']))


async def connect(cfg, forever=False):
    client = make_client(cfg, forever)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise NotLoggedIn('Telegram oturumu kapanmış')
    return client


def parse_meta(text):
    if not text:
        return None
    i = text.find(META_TAG + '|fp=')
    if i < 0:
        return None
    fields = {}
    for part in text[i:].split('\n', 1)[0].split('|')[1:]:
        k, _, v = part.partition('=')
        fields[k] = v
    try:
        idx, n = fields['p'].split('/')
        return {'fp': fields['fp'], 'idx': int(idx) - 1, 'nparts': int(n), 'offset': int(fields['o']),
                'length': int(fields['l']), 'size': int(fields['s']), 'sha': fields['h'],
                'taken': int(fields.get('t') or 0), 'game': fields.get('g') or 'Diğer', 'name': fields.get('n') or ''}
    except (KeyError, ValueError):
        return None


def build_caption(row, fp, idx, nparts, offset, length, size, sha):
    esc = lambda s: html.escape(s, quote=False)  # noqa: E731
    taken = row['taken'] or row['mtime']
    when = dt.datetime.fromtimestamp(taken)
    game = row['game'] or 'Diğer'
    path = row['path'] if len(row['path']) <= 260 else '…' + row['path'][-259:]
    meta = '|'.join([META_TAG, f'fp={fp}', f'p={idx + 1}/{nparts}', f'o={offset}', f'l={length}', f's={size}',
                     f'h={sha}', f't={int(taken)}', f'g={game}', f'n={os.path.basename(row["path"])}'])
    lines = [
        f'🎮 <b>{esc(game)}</b>',
        f'🗓 {when:%d.%m.%Y %H:%M}',
        f'💾 {fmt_size(size)}' + (f' · parça {idx + 1}/{nparts}' if nparts > 1 else ''),
        f'📂 {esc(path)}',
        f'{hashtag(game)} #y{when:%Y}',
        f'<code>{esc(meta)}</code>',
    ]
    return '\n'.join(lines)


class Status:
    """Arka plandaki işin ne yaptığını durum.json'a yazar (durum komutu okur)."""

    def __init__(self):
        self.data = {'pid': os.getpid(), 'basladi': time.time(), 'surum': VERSION}
        self.last_write = 0.0
        self.window = collections.deque()

    def add_bytes(self, n):
        now = time.time()
        self.window.append((now, n))
        while self.window and self.window[0][0] < now - 20:
            self.window.popleft()
        self.data['gonderilen'] = self.data.get('gonderilen', 0) + n
        self.update()

    def speed(self):
        if len(self.window) < 2:
            return 0.0
        span = max(1.0, time.time() - self.window[0][0])
        return sum(b for _, b in self.window) / span

    def update(self, force=False, **kw):
        self.data.update(kw)
        now = time.time()
        if force or now - self.last_write >= 3:
            self.data['hiz'] = self.speed()
            self.data['zaman'] = now
            try:
                write_json_atomic(STATUS_PATH, self.data)
            except OSError:
                pass
            self.last_write = now


def _minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def planned_mbit(cfg, now=None):
    """Saat planına göre hız sınırı (Mbit). None = sınırsız, 0 = bu saatte yükleme yok."""
    now = now or dt.datetime.now()
    cur = now.hour * 60 + now.minute
    for p in cfg.get('hiz_plani') or []:
        a, b = _minutes(p['baslangic']), _minutes(p['bitis'])
        if (a <= cur < b) if a < b else (cur >= a or cur < b):
            return max(0, p.get('mbit') or 0)
    m = cfg.get('max_hiz_mbit') or 0
    return m if m > 0 else None


class Gate:
    """Oyun açıkken ve plan dışı saatlerde bekletir, hızı saat planına göre sınırlar."""

    def __init__(self, cfg, status):
        self.cfg = cfg
        self.status = status
        self.mbit = -1
        self.rate = None
        self.allow = 0.0
        self.last = time.monotonic()
        self.next_check = 0.0
        self.reason = None
        self.resume_at = 0.0
        self.paused_total = 0.0
        self.names = {n.lower() for n in cfg.get('oyun_exe', [])}
        try:
            self.cfg_mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            self.cfg_mtime = None

    def reload_if_changed(self):
        """ayarlar.json değiştiyse hız/oyun ayarlarını yeniden başlatmadan uygular."""
        try:
            m = os.path.getmtime(CONFIG_PATH)
        except OSError:
            return
        if m == self.cfg_mtime:
            return
        self.cfg_mtime = m
        try:
            new = load_config()
        except Exception as e:
            log.warning('ayarlar.json okunamadı: %s', e)
            return
        for key in ('hiz_plani', 'max_hiz_mbit', 'oyunda_dur', 'tam_ekranda_dur', 'oyun_exe',
                    'oyun_kapaninca_bekle_sn', 'uyku_engelle', 'paralel_parca', 'duraklat',
                    'kaynaklar', 'yeni_klip_gun', 'tarama_dakika', 'uzantilar', 'genel_klasorler',
                    'oyun_takma_adlari', 'oyun_konulari', 'otomatik_guncelle', 'kanal_adi'):
            self.cfg[key] = new.get(key)
        self.names = {n.lower() for n in self.cfg.get('oyun_exe') or []}
        log.info('Ayarlar değişti, yeniden yüklendi')

    async def wait(self, nbytes):
        while True:
            now = time.monotonic()
            if now >= self.next_check:
                self.next_check = now + 5
                self.reload_if_changed()
                mbit = planned_mbit(self.cfg)
                if mbit != self.mbit:
                    log.info('Hız sınırı: %s', 'sınırsız' if mbit is None else
                             'bu saatte yükleme yok' if mbit == 0 else f'{mbit} Mbit')
                    self.mbit = mbit
                    self.rate = mbit * 1e6 / 8 if mbit else None
                reason = await asyncio.to_thread(detect_game, self.names, self.cfg)
                if self.cfg.get('duraklat'):
                    reason = 'elle duraklatıldı'
                if not reason and mbit == 0:
                    reason = 'saat planı: bu saatte yükleme yok'
                if reason:
                    if not self.reason:
                        log.info('Duraklatıldı: %s', reason)
                    self.reason = reason
                    self.resume_at = now + self.cfg.get('oyun_kapaninca_bekle_sn', 60)
                elif self.reason and now >= self.resume_at:
                    log.info('Devam ediliyor')
                    self.reason = None
            if not self.reason:
                break
            keep_awake(False)
            self.status.update(durum='duraklatildi', sebep=self.reason)
            t0 = time.monotonic()
            await asyncio.sleep(5)
            self.paused_total += time.monotonic() - t0
            self.last = time.monotonic()
        if self.status.data.get('durum') != 'yukleniyor':
            self.status.update(force=True, durum='yukleniyor', sebep=None)
        keep_awake(True, self.cfg)
        if self.rate:
            now = time.monotonic()
            self.allow = min(self.rate, self.allow + (now - self.last) * self.rate)
            self.last = now
            self.allow -= nbytes
            if self.allow < 0:
                await asyncio.sleep(-self.allow / self.rate)


class Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def upload_range(ctx, path, offset, length, name):
    """Dosyanın [offset, offset+length) aralığını Telegram'a parça parça (aynı anda birkaç parça) yükler."""
    from telethon import errors, functions, types
    total = (length + PART_SIZE - 1) // PART_SIZE
    big = length > SMALL_FILE
    file_id = random.getrandbits(63)
    sha = hashlib.sha256()
    md5 = None if big else hashlib.md5()
    sem = asyncio.Semaphore(max(1, int(ctx.cfg.get('paralel_parca', 8))))
    failure = []
    tasks = set()

    async def send(i, data):
        try:
            req = (functions.upload.SaveBigFilePartRequest(file_id, i, total, data) if big
                   else functions.upload.SaveFilePartRequest(file_id, i, data))
            err = None
            for attempt in range(10):
                try:
                    if await asyncio.wait_for(ctx.client(req), timeout=180):
                        ctx.status.add_bytes(len(data))
                        return
                    err = RuntimeError(f'parça {i} kabul edilmedi')
                except errors.FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                    continue
                except (errors.UnauthorizedError, errors.BadRequestError):
                    raise
                except (ConnectionError, OSError, asyncio.TimeoutError, errors.RPCError) as e:
                    err = e
                await asyncio.sleep(min(60, 2 ** attempt))
            raise err
        except Exception as e:
            failure.append(e)
        finally:
            sem.release()

    def read_block(f, n):
        b = f.read(n)
        sha.update(b)
        if md5 is not None:
            md5.update(b)
        return b

    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            left = length
            i = 0
            while left > 0:
                block = await asyncio.to_thread(read_block, f, min(READ_BLOCK, left))
                if not block:
                    raise IOError('dosya beklenenden kısa')
                left -= len(block)
                for j in range(0, len(block), PART_SIZE):
                    if failure:
                        raise failure[0]
                    data = block[j:j + PART_SIZE]
                    await ctx.gate.wait(len(data))
                    await sem.acquire()
                    t = asyncio.ensure_future(send(i, data))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                    i += 1
            if tasks:
                await asyncio.gather(*list(tasks))
    finally:
        for t in list(tasks):
            t.cancel()
    if failure:
        raise failure[0]
    if i != total:
        raise RuntimeError('parça sayısı tutmuyor')
    if big:
        return types.InputFileBig(file_id, total, name), sha.hexdigest()
    return types.InputFile(file_id, total, name, md5.hexdigest()), sha.hexdigest()


TOPIC_COLORS = [0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F]


async def topic_for(ctx, game):
    """Oyunun forum konusunu (topic) döndürür; yoksa açar. Forum değilse None."""
    if ctx.cfg.get('kanal_turu') != 'forum' or not ctx.cfg.get('oyun_konulari', True):
        return None
    row = ctx.db.execute('SELECT topic_id FROM topics WHERE game=?', (game,)).fetchone()
    if row:
        return row[0]
    from telethon import functions, types
    r = await ctx.client(functions.messages.CreateForumTopicRequest(
        peer=ctx.channel, title=game[:128], icon_color=random.choice(TOPIC_COLORS),
        random_id=random.getrandbits(63)))
    topic_id = None
    for u in getattr(r, 'updates', []):
        msg = getattr(u, 'message', None)
        if isinstance(msg, types.MessageService) and isinstance(msg.action, types.MessageActionTopicCreate):
            topic_id = msg.id
            break
    if topic_id is None:
        raise RuntimeError(f'"{game}" konusu açılamadı')
    ctx.db.execute('INSERT OR REPLACE INTO topics(game, topic_id, created_at) VALUES (?, ?, ?)',
                   (game, topic_id, time.time()))
    ctx.db.commit()
    log.info('Yeni konu açıldı: %s', game)
    return topic_id


async def send_media(ctx, input_file, name, caption, game, video=None, as_video=False):
    """Yüklenen dosyayı gönderir. as_video: Telegram'da oynatılabilen video (önizlemeli) olarak;
    değilse dosya olarak (varsa yine önizleme resmiyle). İki durumda da dosyanın kendisi aynen saklanır."""
    from telethon import errors, types
    attributes = [types.DocumentAttributeFilename(file_name=name)]
    if as_video:
        mime = 'video/mp4' if name.lower().endswith(('.mp4', '.m4v')) else 'video/quicktime'
        attributes.append(types.DocumentAttributeVideo(
            duration=video['duration'], w=video['w'], h=video['h'], supports_streaming=True))
    else:
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    thumb = None
    if video and video.get('thumb'):
        thumb = await ctx.client.upload_file(video['thumb'], file_name='onizleme.jpg')
    media = types.InputMediaUploadedDocument(
        file=input_file, mime_type=mime, attributes=attributes, force_file=not as_video, thumb=thumb)
    topic = await topic_for(ctx, game)
    try:
        return await asyncio.wait_for(
            ctx.client.send_file(ctx.channel, media, caption=caption, parse_mode='html', reply_to=topic),
            timeout=900)
    except errors.RPCError as e:
        if topic and 'TOPIC' in str(e).upper():
            # konu Telegram'da silinmiş/kapatılmış: bir dahaki denemede yenisi açılsın
            ctx.db.execute('DELETE FROM topics WHERE game=?', (game,))
            ctx.db.commit()
        raise


def check_unchanged(path, st):
    st2 = os.stat(path)
    if st2.st_size != st.st_size or st2.st_mtime != st.st_mtime:
        raise FileChanged(path)


async def upload_blob(ctx, row, fp, st, deadline):
    db = ctx.db
    size = st.st_size
    blob = db.execute('SELECT * FROM blobs WHERE fp=?', (fp,)).fetchone()
    chunk_len = blob['chunk_len'] or MAX_PARTS[ctx.premium] * PART_SIZE
    nparts = (size + chunk_len - 1) // chunk_len
    db.execute('UPDATE blobs SET chunk_len=?, nparts=?, size=? WHERE fp=?', (chunk_len, nparts, size, fp))
    db.commit()
    have = {r['idx'] for r in db.execute('SELECT idx FROM parts WHERE fp=?', (fp,))}
    base = os.path.basename(row['path'])
    video = await asyncio.to_thread(probe_video, row['path'])
    # Tek parça ve mp4/mov ise Telegram'da oynatılan video olarak gider; parçalılar dosya + önizleme resmi.
    as_video = bool(nparts == 1 and video and video['w'] and video['h']
                    and os.path.splitext(base)[1].lower() in STREAMABLE_EXTS)
    sha = None
    for idx in range(nparts):
        if idx in have:
            continue
        offset = idx * chunk_len
        length = min(chunk_len, size - offset)
        name = base if nparts == 1 else f'{base}.{idx + 1:03d}'
        ctx.status.update(force=True, dosya=row['path'], oyun=row['game'] or 'Diğer', parca=f'{idx + 1}/{nparts}',
                          parca_boyut=length, parca_ofset=offset, dosya_boyut=size, gonderilen=0)
        t0 = time.time()
        paused0 = ctx.gate.paused_total
        input_file, sha = await upload_range(ctx, row['path'], offset, length, name)
        check_unchanged(row['path'], st)
        caption = build_caption(row, fp, idx, nparts, offset, length, size, sha)
        msg = await send_media(ctx, input_file, name, caption, row['game'] or 'Diğer', video, as_video)
        got = msg.file.size if msg.file else None
        if got != length:
            raise RuntimeError(f'Telegram\'daki boyut tutmuyor ({got} != {length}), mesaj {msg.id}')
        db.execute('INSERT OR REPLACE INTO parts(fp, idx, nparts, offset, length, sha256, msg_id, uploaded_at) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (fp, idx, nparts, offset, length, sha, msg.id, time.time()))
        db.commit()
        active = max(1.0, time.time() - t0 - (ctx.gate.paused_total - paused0))
        stats = meta_get(db, 'istatistik', {'bayt': 0, 'sn': 0})
        stats['bayt'] += length
        stats['sn'] += active
        meta_set(db, 'istatistik', stats)
        log.info('Yüklendi: %s%s (%s, %s, %s/sn)', base, f' [parça {idx + 1}/{nparts}]' if nparts > 1 else '',
                 fmt_size(length), fmt_duration(active), fmt_size(length / active))
        if idx + 1 < nparts and time.time() > deadline:
            return 'yield'
    db.execute("UPDATE blobs SET status='done', sha256=?, done_at=?, attempts=0, next_try=0, last_error=NULL "
               'WHERE fp=?', (sha if nparts == 1 else None, time.time(), fp))
    db.commit()
    meta_set(db, 'son_basari', time.time())
    return 'done'


async def process_item(ctx, row, deadline):
    db = ctx.db
    path = row['path']
    try:
        st = os.stat(path)
    except OSError:
        return 'missing'
    if st.st_size != row['size'] or st.st_mtime != row['mtime']:
        db.execute('UPDATE files SET size=?, mtime=?, fp=NULL WHERE path=?', (st.st_size, st.st_mtime, path))
        db.commit()
        return 'changed'
    if time.time() - st.st_mtime < STABLE_SECONDS:
        return 'busy'
    fp = row['fp']
    if not fp:
        try:
            fp = await asyncio.to_thread(fingerprint, path, st.st_size)
        except OSError as e:
            log.warning('Okunamadı: %s (%s)', path, e)
            return 'missing'
        db.execute('UPDATE files SET fp=? WHERE path=?', (fp, path))
        db.commit()
    db.execute('INSERT OR IGNORE INTO blobs(fp, size) VALUES (?, ?)', (fp, st.st_size))
    db.commit()
    blob = db.execute('SELECT * FROM blobs WHERE fp=?', (fp,)).fetchone()
    if blob['status'] == 'done':
        return 'dup'
    if blob['next_try'] > time.time():
        return 'later'
    try:
        return await upload_blob(ctx, row, fp, st, deadline)
    except FileChanged:
        log.info('Dosya yüklenirken değişti, sonra tekrar denenecek: %s', path)
        db.execute('UPDATE files SET fp=NULL WHERE path=?', (path,))
        db.commit()
        return 'changed'
    except asyncio.CancelledError:
        raise
    except Exception as e:
        from telethon import errors
        if isinstance(e, errors.UnauthorizedError):
            raise NotLoggedIn(str(e))
        attempts = blob['attempts'] + 1
        delay = min(6 * 3600, 60 * 3 ** min(attempts - 1, 6))
        err = f'{type(e).__name__}: {e}'[:500]
        db.execute('UPDATE blobs SET attempts=?, next_try=?, last_error=? WHERE fp=?',
                   (attempts, time.time() + delay, err, fp))
        db.commit()
        log.warning('Yüklenemedi (%d. deneme, %s sonra tekrar): %s: %s', attempts, fmt_duration(delay), path, err)
        if time.time() - meta_get(db, 'son_basari', ctx.started) > 86400:
            alert(db, 'takildi', 'Klip Kalkanı: yedekleme takıldı',
                  f'24 saattir hiçbir klip yüklenemedi. Son hata: {err[:120]}')
        return 'error'


async def verify(ctx, fix=True, progress=None):
    """Kanalda her parçanın hâlâ durduğunu ve boyutunun doğru olduğunu kontrol eder."""
    db = ctx.db
    rows = db.execute('SELECT fp, idx, msg_id, length FROM parts ORDER BY msg_id').fetchall()
    bad = []
    for i in range(0, len(rows), 100):
        if progress:
            progress(i, len(rows))
        batch = rows[i:i + 100]
        msgs = await ctx.client.get_messages(ctx.channel, ids=[r['msg_id'] for r in batch])
        for r, m in zip(batch, msgs):
            if m is None or m.file is None or m.file.size != r['length']:
                bad.append(r)
    if bad and fix:
        with db:
            for r in bad:
                db.execute('DELETE FROM parts WHERE fp=? AND idx=?', (r['fp'], r['idx']))
                db.execute("UPDATE blobs SET status='pending', next_try=0 WHERE fp=?", (r['fp'],))
        log.warning('Doğrulama: %d parça kanalda yok/bozuk, tekrar yüklenecek', len(bad))
    else:
        log.info('Doğrulama: %s parçanın hepsi kanalda duruyor', fmt_count(len(rows)))
    meta_set(db, 'son_dogrulama', time.time())
    return len(rows), len(bad)


# ---------------------------------------------------------------- kurulum yardımcıları

FOLDERID_VIDEOS = '{18989B1D-99B5-455B-841C-AB7C74E4DDFC}'


def known_folder(guid):
    """Windows'un bilinen klasör yolunu (Videolar vb.) döndürür; taşınmış olsa bile doğru yeri verir."""
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16), ('Data3', ctypes.c_uint16),
                    ('Data4', ctypes.c_ubyte * 8)]

    u = uuid.UUID(guid)
    g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    p = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return p.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


def medal_folders():
    """Medal'ın kendi ayarındaki klip klasörü (ClipFolder)."""
    import glob
    import shutil
    import tempfile
    out = []
    for dbp in glob.glob(os.path.join(os.environ.get('APPDATA', ''), 'Medal', 'medal-*.db')):
        if dbp.lower().endswith('medal-guest.db'):
            continue
        tmp = os.path.join(tempfile.gettempdir(), 'klip_kalkani_medal.db')
        row = None
        try:
            shutil.copyfile(dbp, tmp)  # Medal açıkken kilitlememek için kopyasından oku
            con = sqlite3.connect(tmp)
            try:
                row = con.execute("SELECT value FROM key_values WHERE key='ClipFolder'").fetchone()
            finally:
                con.close()
        except Exception:
            pass
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if row:
            raw = row[0] if isinstance(row[0], bytes) else str(row[0]).encode('utf-8')
            m = re.search(rb'([A-Za-z]:[\\/][^\x00-\x1f"]*)', raw)
            if m:
                out.append(os.path.normpath(m.group(1).decode('utf-8', 'ignore').replace('\\\\', '\\')))
    return out


def nvidia_folder():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS') as k:
            v, _ = winreg.QueryValueEx(k, 'DefaultPathW')
        if isinstance(v, bytes):
            v = v.decode('utf-16-le', 'ignore')
        v = v.strip('\x00').strip()
        return os.path.normpath(v) if v else None
    except OSError:
        return None


def obs_folders():
    import configparser
    import glob
    out = []
    pattern = os.path.join(os.environ.get('APPDATA', ''), 'obs-studio', 'basic', 'profiles', '*', 'basic.ini')
    for ini in glob.glob(pattern):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read(ini, encoding='utf-8-sig')
        except Exception:
            continue
        for sec, key in (('SimpleOutput', 'FilePath'), ('AdvOut', 'RecFilePath'), ('AdvOut', 'FFFilePath')):
            if cp.has_option(sec, key) and cp.get(sec, key).strip():
                out.append(os.path.normpath(cp.get(sec, key).strip()))
    return out


def detect_clip_folders():
    """Bu bilgisayardaki klip kaydedicilerin (Medal, NVIDIA, Outplayed, OBS, Xbox, AMD) klasörlerini bulur."""
    videos = known_folder(FOLDERID_VIDEOS) or os.path.join(os.path.expanduser('~'), 'Videos')
    cands = [('Medal', _src(p, 1, haric=['Thumbnails', 'Databases'])) for p in medal_folders()]
    cands.append(('Medal', _src(os.path.join(videos, 'Medal'), 1, haric=['Thumbnails', 'Databases'])))
    for p in filter(None, [nvidia_folder(), videos]):
        cands.append(('NVIDIA', _src(p, 1, desen=NVIDIA_PATTERN, haric=['Medal', 'Thumbnails'])))
    cands.append(('Outplayed', _src(os.path.join(videos, 'Overwolf', 'Outplayed'), 1)))
    cands.append(('Outplayed', _src(os.path.join(videos, 'Outplayed'), 1)))
    cands += [('OBS', _src(p, 1, alt_klasorler=False)) for p in obs_folders()]
    cands.append(('Xbox Game Bar', _src(os.path.join(videos, 'Captures'), 1)))
    cands.append(('AMD ReLive', _src(os.path.join(videos, 'Radeon ReLive'), 1)))
    seen = set()
    out = []
    for label, s in cands:
        key = (os.path.normcase(os.path.normpath(s['yol'])), s.get('desen'))
        if key in seen or not os.path.isdir(s['yol']):
            continue
        seen.add(key)
        out.append((label, s))
    return out


def count_videos(src, exts=None, limit=200000):
    exts = exts or {'.mp4', '.mkv', '.mov', '.webm', '.avi'}
    n = size = 0
    try:
        for p in iter_source(src, exts):
            try:
                size += os.path.getsize(p)
            except OSError:
                continue
            n += 1
            if n >= limit:
                break
    except OSError:
        pass
    return n, size


def measure_upload_mbit():
    """İnternet upload hızını Cloudflare'in hız testi adresine rastgele veri göndererek ölçer (Mbit/sn)."""
    import urllib.request

    def once(nbytes):
        req = urllib.request.Request('https://speed.cloudflare.com/__up', data=os.urandom(nbytes), method='POST',
                                     headers={'Content-Type': 'application/octet-stream'})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        return nbytes * 8 / max(0.001, time.time() - t0) / 1e6

    first = once(4 * 1024 * 1024)
    return max(first, once(min(64, max(8, int(first * 6 / 8))) * 1024 * 1024))


def speed_plan_for(up_mbit):
    day, night = max(1, int(up_mbit * 0.4)), max(1, int(up_mbit * 0.9))
    return [{'baslangic': '01:00', 'bitis': '09:00', 'mbit': night},
            {'baslangic': '09:00', 'bitis': '01:00', 'mbit': day}]


def pythonw_path():
    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return pyw if os.path.exists(pyw) else sys.executable


def register_task(start=True):
    """Windows açılınca (ve yarım saatte bir kontrol ederek) arka planda çalışan görevi kurar ve başlatır."""
    from xml.sax.saxutils import escape
    user = '\\'.join(filter(None, [os.environ.get('USERDOMAIN'), os.environ.get('USERNAME')]))
    start = dt.datetime.now().replace(microsecond=0).isoformat()
    script = os.path.abspath(__file__)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Klip Kalkani: oyun kliplerini Telegram arsivine yedekler</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT30M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT5M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw_path())}</Command>
      <Arguments>-E -s "{escape(script)}" calis</Arguments>
      <WorkingDirectory>{escape(BASE)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    path = os.path.join(BASE, 'gorev.xml')
    with open(path, 'w', encoding='utf-16') as f:
        f.write(xml)
    try:
        r = subprocess.run(['schtasks', '/Create', '/TN', TASK_NAME, '/XML', path, '/F'], capture_output=True,
                           text=True, creationflags=0x08000000)
    finally:
        os.remove(path)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    if start:
        start_task()


def task_state():
    """'yok', 'kapali' ya da 'acik'."""
    r = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME, '/XML'], capture_output=True,
                       creationflags=0x08000000)
    if r.returncode != 0:
        return 'yok'
    b = r.stdout
    text = b.decode('utf-16') if b[:2] in (b'\xff\xfe', b'\xfe\xff') else b.decode('utf-8', 'replace')
    m = re.search(r'<Settings>.*?<Enabled>(\w+)</Enabled>', text, re.S)
    return 'kapali' if m and m.group(1).lower() == 'false' else 'acik'


def start_task():
    subprocess.run(['schtasks', '/Change', '/TN', TASK_NAME, '/ENABLE'], capture_output=True,
                   creationflags=0x08000000)
    subprocess.run(['schtasks', '/Run', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)


def run_task():
    """Görevi şimdi çalıştırır (kapalıysa açmaz)."""
    subprocess.run(['schtasks', '/Run', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)


def remove_task():
    """Otomatik başlatmayı tamamen siler (programı kaldırırken)."""
    subprocess.run(['schtasks', '/End', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)
    r = subprocess.run(['schtasks', '/Delete', '/TN', TASK_NAME, '/F'], capture_output=True, creationflags=0x08000000)
    return r.returncode == 0


def task_folder():
    """Görevin çalıştırdığı program klasörü (başka klasördeki eski kurulumu bulmak için)."""
    r = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME, '/XML'], capture_output=True, creationflags=0x08000000)
    if r.returncode != 0:
        return None
    b = r.stdout
    text = html.unescape(b.decode('utf-16') if b[:2] in (b'\xff\xfe', b'\xfe\xff') else b.decode('mbcs', 'replace'))
    m = re.search(r'<Arguments>[^<]*?"([^"<]+?klip_kalkani\.py)"', text, re.I)
    return os.path.dirname(m.group(1)) if m else None


def restart_task():
    """Arka plandaki yükleyiciyi kapatıp yeniden başlatır (diskteki yeni kodla açılsın diye)."""
    subprocess.run(['schtasks', '/End', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)
    subprocess.run(['schtasks', '/Run', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)


# ---------------------------------------------------------------- otomatik güncelleme (GitHub)

GUNCELLEME_REPO = 'GroundPower/klip-kalkani'  # herkese açık repo; boş bırakılırsa güncelleme kapalı
GUNCELLENEN_DOSYALAR = ('klip_kalkani.py', 'arayuz.pyw', 'duzenle.py', 'kalkan.ico', 'Klip Kalkanı.exe')


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v))[:4])


def _http_get(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': f'KlipKalkani/{VERSION}', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_dev_copy():
    """git ile çalışılan geliştirme kopyası mı? (Kendini güncellemez, değişiklikler git ile gelir.)"""
    return os.path.isdir(os.path.join(BASE, '.git'))


def is_newer(v):
    return _version_tuple(v) > _version_tuple(VERSION)


def fetch_manifest():
    """GitHub'daki son sürümün bilgisi (surum.json). Sondaki ?t= önbellekteki eski kopyayı atlatır."""
    url = f'https://raw.githubusercontent.com/{GUNCELLEME_REPO}/main/surum.json?t={int(time.time())}'
    return json.loads(_http_get(url))


def missing_files(m):
    """Sürüm aynı ama sonradan eklenen bir program dosyası (ör. Klip Kalkanı.exe) bu kurulumda yok mu?"""
    return [n for n in m['dosyalar'] if n in GUNCELLENEN_DOSYALAR and not os.path.exists(os.path.join(BASE, n))]


def check_update():
    """GitHub'da daha yeni sürüm (ya da eksik program dosyası) varsa bilgisini (surum.json) döndürür, yoksa None.
    Geliştirme kopyasında (.git klasörü olan yerde) bakmaz."""
    if not GUNCELLEME_REPO or is_dev_copy():
        return None
    m = fetch_manifest()
    return m if is_newer(m['surum']) or missing_files(m) else None


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def apply_update(m):
    """Yeni sürüm dosyalarını indirir, SHA-256 ile doğrular, sonra yerine koyar. Eskiler eski_surum/'da kalır."""
    import importlib.util
    missing = [mod for mod in m.get('gereken_moduller', []) if importlib.util.find_spec(mod) is None]
    if missing:
        raise RuntimeError('Yeni sürüm ek paket istiyor (' + ', '.join(missing) + '); zip\'i yeniden indir.')
    import urllib.parse
    tag = m.get('etiket') or f"v{m['surum']}"
    tmp = os.path.join(BASE, 'guncelleme')
    os.makedirs(tmp, exist_ok=True)
    got = {}
    for name, sha in m['dosyalar'].items():
        if name not in GUNCELLENEN_DOSYALAR:  # sadece bilinen program dosyaları güncellenir
            continue
        dst = os.path.join(BASE, name)
        if os.path.exists(dst) and file_sha256(dst) == sha:  # bu dosya değişmemiş
            continue
        data = _http_get(f'https://raw.githubusercontent.com/{GUNCELLEME_REPO}/{tag}/{urllib.parse.quote(name)}')
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError(f'{name} doğrulanamadı (özet tutmuyor), güncelleme yapılmadı')
        if name.endswith(('.py', '.pyw')):
            compile(data.decode('utf-8'), name, 'exec')  # sözdizimi bozuksa kurma
        path = os.path.join(tmp, name)
        with open(path, 'wb') as f:
            f.write(data)
        got[name] = path
    backup = os.path.join(BASE, 'eski_surum')
    os.makedirs(backup, exist_ok=True)
    for name, path in got.items():
        dst = os.path.join(BASE, name)
        if os.path.exists(dst):
            shutil.copy2(dst, os.path.join(backup, name))
        try:
            os.replace(path, dst)
        except PermissionError:  # o an açık olan exe'nin üstüne yazılamaz ama adı değiştirilebilir
            os.replace(dst, dst + '.eski')
            os.replace(path, dst)
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return m['surum']


def restart_uploader():
    """Güncellemeden sonra arka plandaki yükleyiciyi yeni kodla yeniden başlatır."""
    cmd = f'cmd /c ping -n 6 127.0.0.1 >nul & schtasks /Run /TN "{TASK_NAME}"'
    for flags in (0x08000000 | 0x01000000, 0x08000000):  # pencere yok (+ görev kutusundan kopar)
        try:
            subprocess.Popen(cmd, creationflags=flags, close_fds=True)
            break
        except OSError:
            continue
    os._exit(0)


def uploader_alive(status_path=None):
    s = read_json(status_path or STATUS_PATH, {}) or {}
    pid = s.get('pid')
    if not pid or not psutil.pid_exists(pid) or time.time() - s.get('zaman', 0) > 180:
        return False
    try:
        return 'python' in psutil.Process(pid).name().lower()
    except psutil.Error:
        return False


def stop_uploader(wait=20, status_path=None):
    """Arka plandaki yükleyiciyi kapatır. Görev açık kalırsa yarım saatlik kontrolde yine başlar;
    tamamen durması için disable_task() da gerekir."""
    subprocess.run(['schtasks', '/End', '/TN', TASK_NAME], capture_output=True, creationflags=0x08000000)
    pid = (read_json(status_path or STATUS_PATH, {}) or {}).get('pid')
    if not pid:
        return
    try:
        p = psutil.Process(pid)
        if 'python' in p.name().lower() and any('klip_kalkani' in a.lower() for a in p.cmdline()):
            p.terminate()
            p.wait(wait)
    except psutil.Error:
        pass


def disable_task():
    subprocess.run(['schtasks', '/Change', '/TN', TASK_NAME, '/DISABLE'], capture_output=True,
                   creationflags=0x08000000)


# ---------------------------------------------------------------- komutlar

async def cmd_login(cfg):
    from telethon import errors
    print('Klip Kalkanı: Telegram girişi\n')
    if not cfg.get('api_id') or not cfg.get('api_hash'):
        cfg['api_id'] = int(input('api_id: ').strip())
        cfg['api_hash'] = input('api_hash: ').strip()
        save_config(cfg)
    client = new_client(SESSION_BASE, cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            phone = input('Telefon numaran (başında +90 ile): ').strip().replace(' ', '')
            sent = await client.send_code_request(phone)
            while True:
                code = input('Telegram uygulamana gelen kod: ').strip().replace(' ', '')
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                    break
                except errors.PhoneCodeInvalidError:
                    print('Kod yanlış, tekrar dene.')
                except errors.PhoneCodeExpiredError:
                    sent = await client.send_code_request(phone)
                    print('Kodun süresi dolmuş, yenisi gönderildi.')
                except errors.SessionPasswordNeededError:
                    while True:
                        try:
                            pw = getpass.getpass('İki adımlı doğrulama şifren (yazarken görünmez): ')
                        except Exception:
                            pw = input('İki adımlı doğrulama şifren: ')
                        try:
                            await client.sign_in(password=pw)
                            break
                        except errors.PasswordHashInvalidError:
                            print('Şifre yanlış, tekrar dene.')
                    break
        me = await client.get_me()
        print(f'\nGiriş tamam: {me.first_name} (Premium: {"evet" if me.premium else "hayır"})')
        await ensure_archive(client, cfg)
        remember_account(account_info(me))
        print('\nHer şey hazır. Bu pencereyi kapatabilirsin.')
    finally:
        await client.disconnect()


async def find_archive(client, cfg):
    """Kayıtlı arşiv grubunu bu hesapla bulur; bulamazsa None. Grubun erişim anahtarı hesaba göre değişir:
    grup bu hesabın sohbetlerinde başka anahtarla duruyorsa yenisi ayarlara yazılır."""
    from telethon import errors, functions, types
    cid, chash = int(cfg['kanal_id']), int(cfg.get('kanal_hash') or 0)
    try:
        r = await client(functions.channels.GetChannelsRequest([types.InputChannel(cid, chash)]))
        ch = next((c for c in r.chats if c.id == cid), None)
        if isinstance(ch, types.Channel) and not ch.left:
            if ch.access_hash and not ch.min and ch.access_hash != chash:
                cfg['kanal_hash'] = ch.access_hash
                save_config(cfg)
            return ch
    except errors.BadRequestError:  # CHANNEL_INVALID / CHANNEL_PRIVATE: bu hesapla bu anahtar geçmiyor
        pass
    async for d in client.iter_dialogs():
        e = d.entity
        if isinstance(e, types.Channel) and e.id == cid and not e.left:
            if e.access_hash != chash:
                cfg['kanal_hash'] = e.access_hash
                save_config(cfg)
            return e
    return None


async def ensure_archive(client, cfg, allow_new=True):
    """Arşiv grubunu hazırlar: sadece senin olduğun, her oyuna ayrı konu açılan gizli forum grubu.
    Dönüş: 'hazir' (kayıtlı grup bu hesapla açılıyor), 'yeni' (yeni grup açıldı) ya da 'erisim_yok' (kayıtlı gruba
    bu hesapla ulaşılamıyor ve allow_new=False; örneğin başka bir hesapla giriş yapılmış)."""
    from telethon import functions, types
    if cfg.get('kanal_id') and cfg.get('kanal_turu') == 'forum':
        ch = await find_archive(client, cfg)
        if ch is not None:
            print(f'Arşiv grubu hazır: {ch.title}')
            return 'hazir'
        if not allow_new:
            return 'erisim_yok'
        print('Kayıtlı arşiv grubuna bu hesapla ulaşılamadı, yenisi açılacak.')
        cfg.setdefault('eski_kanallar', []).append({'id': cfg['kanal_id'], 'hash': cfg.get('kanal_hash'),
                                                   'hesap': (cfg.get('hesap') or {}).get('id')})
    elif cfg.get('kanal_id') and cfg.get('kanal_hash'):
        # Eski (konusuz) kanal: silmiyoruz, sadece adını değiştiriyoruz ki karışmasın.
        try:
            await client(functions.channels.EditTitleRequest(
                types.InputChannel(int(cfg['kanal_id']), int(cfg['kanal_hash'])), 'Klip Arşivi (eski, boş)'))
        except Exception as e:
            log.warning('Eski kanalın adı değiştirilemedi: %s', e)
        cfg.setdefault('eski_kanallar', []).append({'id': cfg['kanal_id'], 'hash': cfg['kanal_hash']})
    old = cfg.get('kanal_id')
    r = await client(functions.channels.CreateChannelRequest(
        title=cfg['kanal_adi'], about=ABOUT, megagroup=True, forum=True))
    ch = next(c for c in r.chats if isinstance(c, types.Channel))
    if not getattr(ch, 'forum', False):
        await client(functions.channels.ToggleForumRequest(types.InputChannel(ch.id, ch.access_hash), True, False))
    cfg['kanal_id'] = ch.id
    cfg['kanal_hash'] = ch.access_hash
    cfg['kanal_turu'] = 'forum'
    save_config(cfg)
    if old:
        bak = reset_upload_state(old)
        if bak:
            print(f'Klipler yeni gruba baştan yüklenecek. Eski kayıtlar: {os.path.basename(bak)}')
    peer = types.InputPeerChannel(ch.id, ch.access_hash)
    msg = await client.send_file(peer, os.path.abspath(__file__), caption=README, parse_mode='html',
                                 force_document=True)
    await client.pin_message(peer, msg, notify=False)
    cfg['readme_msg_id'] = msg.id
    save_config(cfg)
    print(f'Gizli arşiv grubu açıldı: {cfg["kanal_adi"]} (her oyun kendi konusunda)')
    return 'yeni'


def reset_upload_state(old_channel):
    """Arşiv grubu değişti (başka hesap ya da grup silinmiş): klipler yeni gruba baştan yüklenecek.
    Önce veritabanının kopyası alınır; tarama bilgisi (dosyalar ve parmak izleri) aynen kalır."""
    if not os.path.exists(DB_PATH):
        return None
    db = open_db()
    try:
        if not db.execute('SELECT 1 FROM parts UNION ALL SELECT 1 FROM topics LIMIT 1').fetchone():
            return None
        bak = os.path.join(BASE, f'klip_kalkani.{old_channel}.{time.strftime("%Y%m%d-%H%M%S")}.db')
        dst = sqlite3.connect(bak)
        try:
            db.backup(dst)
        finally:
            dst.close()
        with db:
            db.execute('DELETE FROM parts')
            db.execute('DELETE FROM topics')
            db.execute("UPDATE blobs SET status='pending', nparts=NULL, chunk_len=NULL, sha256=NULL, done_at=NULL, "
                       'attempts=0, next_try=0, last_error=NULL')
            db.execute("DELETE FROM meta WHERE k IN ('ilk_yedek_bitti', 'son_basari', 'son_dogrulama')")
        return bak
    finally:
        db.close()


def account_info(me):
    phone = me.phone or ''
    return {'id': me.id, 'ad': ' '.join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id),
            'kullanici': me.username, 'tel_son': phone[-4:] or None, 'premium': bool(getattr(me, 'premium', False))}


def remember_account(info):
    """Arşiv grubunun sahibi olan hesabı ayarlara yazar (pencerede gösterilir)."""
    cfg = load_config()
    cfg['hesap'] = info
    save_config(cfg)
    return info


def install_login_session():
    """Pencereden yapılan giriş tamamlandı: geçici oturum dosyası asıl yerine geçer."""
    src = LOGIN_SESSION_BASE + '.session'
    if os.path.exists(src):
        os.replace(src, SESSION_FILE)


async def telegram_logout(prepare=None):
    """Bu bilgisayardaki Telegram girişini kapatır: oturum Telegram'daki cihaz listesinden de düşer ve oturum
    dosyası silinir. Arşiv grubuna ve kliplere dokunmaz. prepare: Telegram'a bağlanıldıktan sonra, çıkıştan hemen
    önce çalışır (yükleyiciyi durdurmak için); internet yoksa hiçbir şey değişmeden hata verir."""
    cfg = load_config()
    for base in (SESSION_BASE, LOGIN_SESSION_BASE):
        path = base + '.session'
        if not os.path.exists(path):
            continue
        client = new_client(base, cfg)
        try:
            await client.connect()
            if prepare:
                await asyncio.to_thread(prepare)
                prepare = None
            if await client.is_user_authorized():
                await client.log_out()  # başarılıysa oturum dosyasını da siler
        finally:
            if client.session is not None:
                await client.disconnect()
        if os.path.exists(path):  # oturum zaten kapanmışsa (ya da çıkış reddedildiyse) işe yaramaz, kenara al
            os.replace(path, path + '.eski')
    if prepare:
        await asyncio.to_thread(prepare)


def _has_install(d):
    """Bu klasörde kurulumu bitmiş (arşiv grubu ve klip klasörleri belli) Klip Kalkanı verisi var mı?"""
    cfg = read_json(os.path.join(d, 'ayarlar.json'), {}) or {}
    return bool(cfg.get('kanal_id') and cfg.get('kaynaklar'))


def old_install(other_folders=True):
    """%APPDATA%'da henüz kurulu veri yokken, 1.7 öncesi bir kurulumun verisi nerede? (Önce bu program klasörü,
    sonra görevin çalıştırdığı klasör.) Yoksa None."""
    if _has_install(APPDATA_DIR):
        return None
    candidates = [BASE] + ([task_folder()] if other_folders else [])
    for d in candidates:
        if d and not _same(d, APPDATA_DIR) and os.path.exists(os.path.join(d, 'ayarlar.json')):
            return d
    return None


def migrate_data(source=None, stop_running=False):
    """Eski kurulumun ayarlarını, Telegram girişini, veritabanını ve günlüklerini %APPDATA%\\KlipKalkani'ye taşır.
    Önce hepsi kopyalanır (veritabanı SQLite yedeklemesiyle), ayarlar.json en son; ancak ondan sonra eskiler
    kaldırılır. Kopyalama yarıda kalırsa hiçbir şey değişmez, eski yerden devam edilir. Dönüş: taşındıysa True."""
    src = source or old_install(other_folders=False)
    if not src or _has_install(APPDATA_DIR):  # yarım kalmış yeni kurulum (ör. sadece API bilgisi) üstüne yazılır
        return False
    status = os.path.join(src, 'durum.json')
    was_running = stop_running and uploader_alive(status)
    if was_running:  # eski kodla çalışan yükleyici veritabanını açık tutuyor
        stop_uploader(status_path=status)
    try:
        new = APPDATA_DIR
        os.makedirs(new, exist_ok=True)
        moved = []
        src_db = os.path.join(src, 'klip_kalkani.db')
        if os.path.exists(src_db):
            s = sqlite3.connect(src_db, timeout=60)
            d = sqlite3.connect(os.path.join(new, 'klip_kalkani.db'))
            try:
                s.backup(d)
            finally:
                d.close()
                s.close()
            moved += [src_db + x for x in ('', '-wal', '-shm')]
        for pattern in DATA_PATTERNS:
            for f in glob.glob(os.path.join(src, pattern)):
                shutil.copy2(f, os.path.join(new, os.path.basename(f)))
                moved.append(f)
        if os.path.isdir(os.path.join(src, 'log')):
            shutil.copytree(os.path.join(src, 'log'), os.path.join(new, 'log'), dirs_exist_ok=True)
            moved.append(os.path.join(src, 'log'))
        shutil.copy2(os.path.join(src, 'ayarlar.json'), os.path.join(new, 'ayarlar.json'))  # en son: taşıma tamam
        moved.append(os.path.join(src, 'ayarlar.json'))
        set_data_dir(new)
        for f in moved:  # kopyaları yeni yerde; eski yerdekiler kaldırılır
            try:
                if os.path.isdir(f):
                    shutil.rmtree(f)
                elif os.path.exists(f):
                    os.remove(f)
            except OSError as e:
                log.warning('Eski dosya kaldırılamadı: %s (%s)', f, e)
    finally:
        if was_running and task_state() == 'acik':
            run_task()
    return True


def adopt_old_install(src):
    """Başka klasördeki eski kurulumun verisini alır ve otomatik başlatmayı bu klasöre çevirir (açık/kapalı durumu
    korunur). Pencerenin kurulum ekranındaki "buraya al" düğmesi."""
    state = task_state()
    if not migrate_data(src, stop_running=True):
        raise RuntimeError('taşınacak ayar bulunamadı')
    if state != 'yok':
        stop_uploader()
        register_task(start=False)
        if state == 'kapali':
            disable_task()
        elif not load_config().get('duraklat'):
            run_task()


def request_scan():
    """Arka plandaki yükleyiciye "hemen tara" der (sıradaki dosyadan önce, en geç bir dakikada tarar)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SCAN_REQUEST, 'w', encoding='utf-8') as f:
        f.write(str(time.time()))


def stop_backup():
    """Yükleyiciyi ve otomatik başlatmayı kapatır (çıkış yaparken)."""
    stop_uploader()
    disable_task()


async def cmd_logout():
    if not os.path.exists(SESSION_FILE):
        print('Bu bilgisayarda Telegram girişi yok.')
        return
    await telegram_logout(prepare=stop_backup)
    print("Telegram'dan çıkış yapıldı; yedekleme durdu. Telegram'daki arşiv grubu ve klipler olduğu gibi duruyor.")
    print('Tekrar başlatmak için Klip Kalkanı penceresini açıp giriş yap.')


def cmd_uninstall():
    print('Otomatik başlatma kaldırıldı.' if remove_task() else 'Otomatik başlatma zaten kurulu değil.')
    print("Bilgisayardaki dosyalara ve Telegram'daki yedeklere dokunulmadı.")


async def cmd_run(cfg, limit=None):
    if not single_instance():
        log.info('Zaten çalışıyor, ikinci kopya açılmadı.')
        return
    db = open_db()
    status = Status()
    started = time.time()
    log.info('Klip Kalkanı %s başladı', VERSION)
    if load_config().get('telegramsiz'):
        log.info("Telegram'sız kullanım seçili: yedekleme kapalı, yükleyici çalışmıyor")
        status.update(force=True, durum='telegramsiz', sebep=None)
        return
    if not meta_get(db, 'kurulum'):
        meta_set(db, 'kurulum', time.time())
    while True:
        cfg = load_config()
        try:
            client = await connect(cfg, forever=True)
        except NotLoggedIn as e:
            status.update(force=True, durum='giris_gerekli', sebep=str(e))
            alert(db, 'giris', 'Klip Kalkanı: giriş gerekli',
                  f'Telegram oturumu kapalı ({e}). Klip Kalkanı penceresinden tekrar giriş yap.', 24)
            if limit:
                return
            await asyncio.sleep(1800)
            continue
        except Exception as e:
            log.warning('Telegram\'a bağlanılamadı: %s', e)
            status.update(force=True, durum='baglanti_yok', sebep=str(e))
            await asyncio.sleep(60)
            continue
        try:
            await upload_loop(client, cfg, db, status, limit, started)
            if limit:
                return
        except NotLoggedIn as e:
            status.update(force=True, durum='giris_gerekli', sebep=str(e))
            alert(db, 'giris', 'Klip Kalkanı: giriş gerekli',
                  f'Telegram oturumu kapanmış ({e}). Klip Kalkanı penceresinden tekrar giriş yap.', 24)
            await asyncio.sleep(1800)
        except Exception as e:
            log.exception('Beklenmeyen hata: %s', e)
            status.update(force=True, durum='hata', sebep=str(e))
            await asyncio.sleep(60)
        finally:
            keep_awake(False)
            await client.disconnect()


async def upload_loop(client, cfg, db, status, limit, started):
    me = await client.get_me()
    ctx = Ctx(client=client, cfg=cfg, db=db, status=status, premium=bool(getattr(me, 'premium', False)),
              channel=channel_peer(cfg), started=started, next_update_check=time.time() + 120)
    ctx.gate = Gate(cfg, status)
    log.info('Bağlandı: %s (Premium: %s)', me.first_name, 'evet' if ctx.premium else 'hayır')
    next_scan = 0.0
    queue = []
    done = 0
    worked = False
    while True:
        now = time.time()
        if os.path.exists(SCAN_REQUEST):  # pencereden "şimdi tara" istendi
            try:
                os.remove(SCAN_REQUEST)
            except OSError:
                pass
            next_scan = 0.0
        if now >= next_scan:
            ctx.gate.reload_if_changed()  # klasör listesi arayüzden değişmiş olabilir
            st = await asyncio.to_thread(scan, cfg)
            queue = build_queue(db, cfg, st['start'])
            next_scan = time.time() + cfg['tarama_dakika'] * 60
            if st['new'] or st['changed']:
                log.info('Tarama: %d yeni, %d değişmiş dosya; kuyrukta %d klip', st['new'], st['changed'], len(queue))
            if st['unavailable']:
                since = meta_get(db, 'erisilemez_baslangic') or now
                meta_set(db, 'erisilemez_baslangic', since)
                if now - since > 7 * 86400:
                    alert(db, 'erisilemez', 'Klip Kalkanı: klasöre ulaşılamıyor',
                          'Şu klasörler bir haftadır yok: ' + ', '.join(st['unavailable'][:3]), 7 * 24)
            else:
                meta_set(db, 'erisilemez_baslangic', None)
        if now >= (meta_get(db, 'son_dogrulama') or 0) + 7 * 86400 and db.execute(
                'SELECT 1 FROM parts LIMIT 1').fetchone():
            await verify(ctx)
        if now >= ctx.next_update_check and cfg.get('otomatik_guncelle', True):
            ctx.next_update_check = now + 6 * 3600
            try:
                m = await asyncio.to_thread(check_update)
                if m:
                    new = await asyncio.to_thread(apply_update, m)
                    log.info('Güncellendi: %s -> %s; yeni sürümle yeniden başlatılıyor', VERSION, new)
                    keep_awake(False)
                    restart_uploader()
            except Exception as e:
                log.warning('Güncelleme denetlenemedi: %s', e)
        if not queue:
            keep_awake(False)
            status.update(force=True, durum='hazir', dosya=None, sebep=None)
            if worked and not meta_get(db, 'ilk_yedek_bitti'):
                meta_set(db, 'ilk_yedek_bitti', time.time())
                try:
                    toast('Klip Kalkanı', 'Bütün klipler yedeklendi. Artık sadece yeni klipler yüklenecek.')
                except Exception:
                    pass
            if limit:
                return
            await asyncio.sleep(max(5.0, min(60.0, next_scan - time.time())))
            continue
        row = queue.pop(0)
        result = await process_item(ctx, row, next_scan)
        if result == 'done':
            done += 1
            worked = True
            if limit and done >= limit:
                return
        elif result == 'yield':
            next_scan = 0.0


async def cmd_try(cfg, path):
    """Tek bir dosyayı hemen yükler (deneme)."""
    path = os.path.normpath(os.path.abspath(path))
    st = os.stat(path)
    db = open_db()
    game, taken = detect_game_date(path, st.st_mtime, {}, naming_rules(cfg))
    db.execute('INSERT OR IGNORE INTO files(path, size, mtime, fp, source, prio, game, taken, seen) '
               'VALUES (?, ?, ?, NULL, ?, 0, ?, ?, ?)',
               (path, st.st_size, st.st_mtime, os.path.dirname(path), game, taken, time.time()))
    db.commit()
    row = db.execute('SELECT * FROM files WHERE path=?', (path,)).fetchone()
    client = await connect(cfg)
    try:
        me = await client.get_me()
        status = Status()
        ctx = Ctx(client=client, cfg=dict(cfg, oyunda_dur=False), db=db, status=status,
                  premium=bool(getattr(me, 'premium', False)), channel=channel_peer(cfg), started=time.time())
        ctx.gate = Gate(ctx.cfg, status)
        row = dict(row)
        row['mtime'] = st.st_mtime
        result = await process_item(ctx, row, time.time() + 86400)
        print('Sonuç:', {'done': 'yüklendi', 'dup': 'zaten yedekli'}.get(result, result))
    finally:
        keep_awake(False)
        await client.disconnect()


def source_summary(db, cfg, since):
    done_fps = {r[0] for r in db.execute("SELECT fp FROM blobs WHERE status='done'")}
    per = collections.OrderedDict((s['yol'], [0, 0, 0, 0]) for s in cfg['kaynaklar'])
    uniq = {}
    for r in db.execute('SELECT path, size, fp, source FROM files WHERE seen >= ? AND size > 0', (since,)):
        a = per.setdefault(r['source'], [0, 0, 0, 0])
        a[0] += 1
        a[1] += r['size']
        if r['fp'] in done_fps:
            a[2] += 1
        else:
            a[3] += 1
        uniq[r['fp'] or r['path']] = (r['size'], r['fp'] in done_fps)
    return per, uniq


def cmd_scan(cfg):
    print('Taranıyor…')
    st = scan(cfg)
    db = open_db()
    rows = db.execute('SELECT path, size FROM files WHERE fp IS NULL AND seen >= ? AND size > 0',
                      (st['start'],)).fetchall()
    if rows:
        print(f'{fmt_count(len(rows))} dosyanın parmak izi çıkarılıyor (tekrar edenleri bulmak için)…')
        t0 = time.time()
        for i, r in enumerate(rows, 1):
            try:
                fp = fingerprint(r['path'], r['size'])
            except OSError as e:
                log.warning('Okunamadı: %s (%s)', r['path'], e)
                continue
            db.execute('UPDATE files SET fp=? WHERE path=?', (fp, r['path']))
            if i % 250 == 0 or i == len(rows):
                db.commit()
                print(f'  {i}/{len(rows)}  ({fmt_duration(time.time() - t0)})', flush=True)
        db.commit()
    per, uniq = source_summary(db, cfg, st['start'])
    print(f'\n{"Klasör":<46} {"Klip":>6} {"Boyut":>10} {"Yedekli":>8} {"Bekliyor":>8}')
    for src, (n, size, ok, wait) in per.items():
        if src in st['unavailable']:
            print(f'{src[:46]:<46} {"— ulaşılamıyor —":>34}')
        elif n:
            print(f'{src[:46]:<46} {fmt_count(n):>6} {fmt_size(size):>10} {fmt_count(ok):>8} {fmt_count(wait):>8}')
    total = sum(s for s, _ in uniq.values())
    ok = sum(s for s, d in uniq.values() if d)
    print(f'\nTekrarlar hariç: {fmt_count(len(uniq))} klip, {fmt_size(total)}. '
          f'Yedekli: {fmt_size(ok)}, bekleyen: {fmt_size(total - ok)}')


def cmd_status(cfg):
    db = open_db()
    since = meta_get(db, 'son_tarama')
    if since is None:
        print('Henüz tarama yapılmadı.')
        return
    done_n, done_b = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs WHERE status='done'").fetchone()
    pend = {}
    errs = []
    for r in pending_rows(db, since - 1):
        key = r['fp'] or r['path']
        pend[key] = r['size']
        if r['attempts'] and r['last_error']:
            errs.append(r)
    partial = db.execute("SELECT COALESCE(SUM(p.length), 0) FROM parts p JOIN blobs b ON b.fp = p.fp "
                         "WHERE b.status != 'done'").fetchone()[0]
    pend_b = max(0, sum(pend.values()) - partial)
    stats = meta_get(db, 'istatistik', {'bayt': 0, 'sn': 0})
    avg = stats['bayt'] / stats['sn'] if stats['sn'] else 0

    s = read_json(STATUS_PATH, {}) or {}
    alive = bool(s.get('pid')) and psutil.pid_exists(s['pid']) and time.time() - s.get('zaman', 0) < 120
    print('Klip Kalkanı: durum\n')
    if not alive:
        installed = subprocess.run(['schtasks', '/Query', '/TN', TASK_NAME], capture_output=True,
                                   creationflags=0x08000000).returncode == 0
        print('Arka plan:   ÇALIŞMIYOR' + (' (otomatik görev kurulu; en geç yarım saat içinde kendiliğinden başlar)'
                                            if installed else ' (otomatik görev kurulu değil)'))
    else:
        d = s.get('durum')
        if d == 'yukleniyor' and s.get('dosya'):
            pct = 100 * s.get('gonderilen', 0) / max(1, s.get('parca_boyut', 1))
            print(f'Arka plan:   yüklüyor: {os.path.basename(s["dosya"])} (parça {s.get("parca")}, '
                  f'%{min(pct, 100):.0f}, {fmt_size(s.get("hiz", 0))}/sn)')
        elif d == 'duraklatildi':
            print(f'Arka plan:   duraklatıldı: {s.get("sebep")}')
        elif d == 'hazir':
            print('Arka plan:   hazır, yeni klip bekliyor')
        else:
            print(f'Arka plan:   {d} {s.get("sebep") or ""}')
    print(f'Yedeklenen:  {fmt_count(done_n)} klip, {fmt_size(done_b)}')
    eta = f', tahmini {fmt_duration(pend_b / avg)} (oyun araları hariç)' if avg and pend_b else ''
    print(f'Bekleyen:    {fmt_count(len(pend))} klip, {fmt_size(pend_b)}{eta}')
    if errs:
        print(f'Sorunlu:     {len(errs)} klip tekrar denenecek. Son hata: {errs[0]["last_error"][:100]}')
    last_ok = meta_get(db, 'son_basari')
    if last_ok:
        print(f'Son yedek:   {fmt_duration(time.time() - last_ok)} önce')
    un = meta_get(db, 'erisilemeyen') or []
    if un:
        print('Ulaşılamayan klasörler (disk takılı değil mi?): ' + ', '.join(un))
    ver = meta_get(db, 'son_dogrulama')
    if ver:
        print(f'Son kontrol: {fmt_duration(time.time() - ver)} önce (kanaldaki yedekler yerinde)')


async def collect_channel(client, peer):
    """Gruptaki klipleri parmak izine göre toplar. Aynı klip birden fazla kez yüklenmişse
    eksiksiz olan en yeni kopya seçilir."""
    sets = {}
    async for m in client.iter_messages(peer):  # yeniden eskiye
        meta = parse_meta(m.message)
        if not meta or not m.file:
            continue
        s = sets.setdefault((meta['fp'], meta['nparts']), {'meta': meta, 'parts': {}, 'newest': m.id})
        s['parts'].setdefault(meta['idx'], (m, meta))
    groups = {}
    for (fp, n), s in sets.items():
        rank = (len(s['parts']) == n, s['newest'])
        cur = groups.get(fp)
        if cur is None or rank > (len(cur['parts']) == cur['meta']['nparts'], cur['newest']):
            groups[fp] = s
    return groups


def unique_path(path):
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f'{root} ({i}){ext}'):
        i += 1
    return f'{root} ({i}){ext}'


DL_REQUEST = 1024 * 1024  # Telegram'ın izin verdiği en büyük indirme parçası
DL_STREAMS = 3            # aynı anda indirilen bölüm sayısı (Telegram hesap başına ~6 MB/sn veriyor)


async def _download_range(client, media, path, base_offset, start, end, progress=None):
    """Telegram'daki dosyanın [start, end) aralığını, diskteki dosyada base_offset + start'a yazar."""
    with open(path, 'r+b') as f:
        f.seek(base_offset + start)
        async for chunk in client.iter_download(media, offset=start, request_size=DL_REQUEST,
                                                limit=-(-(end - start) // DL_REQUEST)):
            f.write(chunk)
            if progress:
                progress(len(chunk))


def _sha_of_range(path, offset, length):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        f.seek(offset)
        while length > 0:
            b = f.read(min(READ_BLOCK, length))
            if not b:
                break
            h.update(b)
            length -= len(b)
    return h.hexdigest()


async def download_group(client, g, dest, progress=None):
    """Bir klibi (tüm parçalarıyla) indirir, her parçayı özetiyle doğrular. progress(n): inen bayt."""
    meta = g['meta']
    n = meta['nparts']
    if len(g['parts']) != n:
        return 'eksik'
    if os.path.exists(dest):
        if os.path.getsize(dest) == meta['size']:
            return 'var'
        dest = unique_path(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + '.indiriliyor'
    with open(tmp, 'wb') as f:
        f.truncate(meta['size'])
    try:
        for idx in range(n):
            m, pm = g['parts'][idx]
            size = pm['length']
            nchunks = -(-size // DL_REQUEST)
            per = -(-nchunks // DL_STREAMS) * DL_REQUEST  # 1 MB'a hizalı bölüm boyu
            ranges = [(a, min(size, a + per)) for a in range(0, size, per)]
            await asyncio.gather(*(_download_range(client, m.media, tmp, pm['offset'], a, b, progress)
                                   for a, b in ranges))
            if await asyncio.to_thread(_sha_of_range, tmp, pm['offset'], size) != pm['sha']:
                raise RuntimeError(f'parça {idx + 1}/{n} bozuk indi (özet tutmuyor)')
    except BaseException:
        # yarım kalan geçici dosyayı (bizim oluşturduğumuz .indiriliyor) bırakma
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)
    return dest


async def cmd_restore(cfg, args):
    interactive = not args.hedef
    if interactive:
        args.hedef = input('Klipler hangi klasöre indirilsin? (örn. D:\\GeriYuklenen): ').strip().strip('"')
        args.oyun = input('Hangi oyun? (boş bırak = hepsi): ').strip() or None
        args.ay = input('Hangi ay? (ör. 2026-07, boş = hepsi): ').strip() or None
        args.ara = input('Adında geçen kelime? (boş = hepsi): ').strip() or None
        args.hepsi = True
    client = await connect(cfg)
    try:
        peer = channel_peer(cfg)
        print('Kanal okunuyor…')
        groups = await collect_channel(client, peer)
        sel = []
        for g in groups.values():
            m = g['meta']
            if args.oyun and norm_key(args.oyun) not in norm_key(m['game']):
                continue
            if args.ay and dt.datetime.fromtimestamp(m['taken']).strftime('%Y-%m') != args.ay:
                continue
            if args.ara and args.ara.lower() not in m['name'].lower():
                continue
            sel.append(g)
        total = sum(g['meta']['size'] for g in sel)
        print(f'{fmt_count(len(sel))} klip bulundu, {fmt_size(total)} (kanalda toplam {fmt_count(len(groups))}).')
        if args.liste:
            for g in sorted(sel, key=lambda g: g['meta']['taken']):
                m = g['meta']
                print(f'  {dt.datetime.fromtimestamp(m["taken"]):%d.%m.%Y %H:%M}  {m["game"]:<22} '
                      f'{fmt_size(m["size"]):>10}  {m["name"]}')
            return
        if not sel:
            return
        if not (args.oyun or args.ay or args.ara or args.hepsi):
            print('Hepsini indirmek için --hepsi ekle (ya da --oyun / --ay / --ara ile daralt).')
            return
        if interactive or not args.evet:
            if input('İndirilsin mi? (e/h): ').strip().lower() not in ('e', 'evet', 'y'):
                return
        ok = fail = 0
        for i, g in enumerate(sorted(sel, key=lambda g: g['meta']['taken']), 1):
            m = g['meta']
            dest = os.path.join(args.hedef, safe_name(m['game']), safe_name(m['name']))
            try:
                t0 = time.time()
                r = await download_group(client, g, dest)
                ok += 1
                el = max(0.1, time.time() - t0)
                done_txt = f'indi ({fmt_size(m["size"])}, {fmt_duration(el)}, {fmt_size(m["size"] / el)}/sn)'
                print(f'[{i}/{len(sel)}] {m["name"]}: '
                      + {'var': 'zaten var', 'eksik': 'EKSİK PARÇA'}.get(r, done_txt), flush=True)
                if r == 'eksik':
                    fail += 1
            except Exception as e:
                fail += 1
                print(f'[{i}/{len(sel)}] {m["name"]}: HATA {e}')
        print(f'\nBitti: {ok - fail} tamam, {fail} sorunlu. Klasör: {args.hedef}')
    finally:
        await client.disconnect()


async def verify_archive(cfg, progress=None):
    """Telegram'daki bütün parçaları kontrol eder; eksik/bozuk olanlar tekrar yüklenmek üzere işaretlenir.
    Dönüş: (kontrol edilen, eksik)."""
    client = await connect(cfg)
    db = open_db()
    try:
        return await verify(Ctx(client=client, db=db, channel=channel_peer(cfg)), progress=progress)
    finally:
        db.close()
        await client.disconnect()


async def cmd_verify(cfg):
    total, bad = await verify_archive(cfg)
    print(f'{fmt_count(total)} parça kontrol edildi, {bad} tanesi eksik/bozuk' + (' (tekrar yüklenecek)' if bad else ''))


async def reindex(cfg):
    """Yerel kayıtları Telegram'daki arşivden yeniden kurar (bilgisayar değişince ya da veritabanı bozulunca).
    Zaten yüklü olanlar tekrar yüklenmez. Dönüş: (kanaldaki klip, eksiksiz olan)."""
    client = await connect(cfg)
    try:
        groups = await collect_channel(client, channel_peer(cfg))
        db = open_db()
        complete = 0
        with db:
            for fp, g in groups.items():
                m = g['meta']
                chunk = g['parts'][0][1]['length'] if m['nparts'] > 1 and 0 in g['parts'] else None
                db.execute('INSERT OR IGNORE INTO blobs(fp, size, nparts, chunk_len) VALUES (?, ?, ?, ?)',
                           (fp, m['size'], m['nparts'], chunk))
                for idx, (msg, pm) in g['parts'].items():
                    db.execute('INSERT OR REPLACE INTO parts(fp, idx, nparts, offset, length, sha256, msg_id, '
                               'uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                               (fp, idx, pm['nparts'], pm['offset'], pm['length'], pm['sha'], msg.id,
                                msg.date.timestamp()))
                if len(g['parts']) == m['nparts']:
                    complete += 1
                    db.execute("UPDATE blobs SET status='done', done_at=?, sha256=? WHERE fp=?",
                               (time.time(), m['sha'] if m['nparts'] == 1 else None, fp))
        db.close()
        return len(groups), complete
    finally:
        await client.disconnect()


async def cmd_reindex(cfg):
    print('Kanal okunuyor…')
    found, complete = await reindex(cfg)
    print(f'Kanalda {fmt_count(found)} klip bulundu, {fmt_count(complete)} tanesi eksiksiz. Veritabanı güncellendi.')


def main():
    for s in (sys.stdout, sys.stderr):
        if s is not None:
            try:
                s.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
    ap = argparse.ArgumentParser(prog='klip_kalkani', description='Oyun kliplerini Telegram\'a yedekler.')
    sub = ap.add_subparsers(dest='komut', required=True)
    sub.add_parser('giris')
    sub.add_parser('tara')
    p = sub.add_parser('calis')
    p.add_argument('--limit', type=int)
    sub.add_parser('durum')
    sub.add_parser('dogrula')
    p = sub.add_parser('geri-yukle')
    p.add_argument('hedef', nargs='?')
    p.add_argument('--oyun')
    p.add_argument('--ay')
    p.add_argument('--ara')
    p.add_argument('--hepsi', action='store_true')
    p.add_argument('--liste', action='store_true')
    p.add_argument('--evet', action='store_true')
    p.add_argument('--bekle', action='store_true', help='bitince pencereyi açık tut')
    sub.add_parser('indeks-yenile')
    p = sub.add_parser('yukle-dene')
    p.add_argument('dosya')
    sub.add_parser('kaldir')
    sub.add_parser('cikis')
    args = ap.parse_args()

    if args.komut == 'calis':
        if not single_instance():
            return  # arka planda zaten bir yükleyici var
        try:
            migrate_data()  # 1.7 öncesi kurulum: veriler program klasöründen %APPDATA%'ya
        except Exception as e:
            print(f'Veriler taşınamadı, eski yerden devam: {e}')
    cfg = load_config()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    setup_logging('klip_kalkani.log' if args.komut == 'calis' else 'komutlar.log', console=True)
    try:
        if args.komut == 'giris':
            asyncio.run(cmd_login(cfg))
        elif args.komut == 'tara':
            cmd_scan(cfg)
        elif args.komut == 'calis':
            asyncio.run(cmd_run(cfg, args.limit))
        elif args.komut == 'durum':
            cmd_status(cfg)
        elif args.komut == 'dogrula':
            asyncio.run(cmd_verify(cfg))
        elif args.komut == 'geri-yukle':
            asyncio.run(cmd_restore(cfg, args))
        elif args.komut == 'indeks-yenile':
            asyncio.run(cmd_reindex(cfg))
        elif args.komut == 'yukle-dene':
            asyncio.run(cmd_try(cfg, args.dosya))
        elif args.komut == 'kaldir':
            cmd_uninstall()
        elif args.komut == 'cikis':
            asyncio.run(cmd_logout())
    except NotLoggedIn as e:
        print(f'Önce giriş yapman lazım ({e}): Klip Kalkanı penceresini aç.')
        sys.exit(2)
    except KeyboardInterrupt:
        print('\nDurduruldu.')
    except Exception as e:
        if not getattr(args, 'bekle', False):
            raise
        print(f'\nHATA: {type(e).__name__}: {e}')
    finally:
        if getattr(args, 'bekle', False):
            try:
                input('\nKapatmak için Enter\'a bas...')
            except EOFError:
                pass


if __name__ == '__main__':
    main()
