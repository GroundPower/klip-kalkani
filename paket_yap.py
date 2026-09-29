# -*- coding: utf-8 -*-
"""Taşınabilir Klip Kalkanı paketi (zip) yapar: Python + kütüphaneler + program. Kurulum gerektirmez.

Kullanım (Windows, Python 3.12 ile):
  python paket_yap.py                         -> dist/KlipKalkani-<sürüm>.zip  (API bilgisi YOK; herkese açık sürüm)
  python paket_yap.py --api varsayilan.json   -> API bilgisi gömülü zip (SADECE güvendiğin kişilere ver)

varsayilan.json: {"api_id": 123, "api_hash": "..."}  (my.telegram.org → API development tools)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_FILES = ['Klip Kalkanı.exe', 'klip_kalkani.py', 'arayuz.pyw', 'duzenle.py', 'kalkan.ico']
SKIP_TOP = {'Lib', 'Scripts', 'include', 'libs', 'Doc', 'Tools', 'share', 'NEWS.txt'}
SKIP_LIB = {'site-packages', 'test', 'idlelib', 'turtledemo', 'ensurepip', 'lib2to3', '__pycache__'}
NO_CACHE = shutil.ignore_patterns('__pycache__', '*.pyc')

README = """KLİP KALKANI — oyun kliplerini Telegram'a otomatik yedekler
================================================================

KURULUM (bir kere, 2 dakika)
1. Bu klasörü kalıcı bir yere koy, örneğin C:\\KlipKalkani ya da D:\\KlipKalkani.
   (Zip'in içinden çalıştırma; önce klasöre çıkar.)
2. "Klip Kalkanı.exe"ye çift tıkla. Kurulum penceresi açılır:
   - Klip klasörlerini (Medal, NVIDIA, Outplayed, OBS, Xbox...) kendisi bulur. "Klasör ekle" ile başkası da eklenir.
   - "İnternet hızımı ölç"e bas, gündüz/gece hız sınırını kendisi seçer.
   - Telefon numaranı yaz (+90...), "Kod gönder"e bas, Telegram uygulamana gelen kodu yazıp "Giriş yap"a bas.
     İki adımlı şifren varsa onu da ister.
   - "Kurulumu bitir"e bas. Bitti! Masaüstüne ve Başlat menüsüne "Klip Kalkanı" kısayolu konur.
3. Windows "bilinmeyen uygulama" uyarısı verirse: "Ek bilgi" > "Yine de çalıştır".

NE YAPAR
- Klipler Telegram'da sadece senin göreceğin gizli "Klip Arşivi" grubuna, her oyun ayrı konuda, orijinal
  kalitede ve önizlemeli video olarak gider. Telefondan da izlenir.
- Bilgisayar her açıldığında arka planda kendiliğinden çalışır, yeni klipleri de yükler.
- Oyun açıkken yükleme durur, ping'in bozulmaz. Gündüz/gece hız sınırı pencereden değişir.
- Düzenle sekmesi: sesli önizleme, kayıpsız kesme, tam kare kesme (NVIDIA), klip birleştirme.
  Ses kanalları (tüm ses / oyun / Discord / mikrofon): aç/kapat, seviye, tek dinle; kaydederken hepsini tek
  kanalda birleştir (Discord'da ve telefonda her şey duyulsun) ya da ayrı kanallar olarak kaydet.
- Hiçbir dosyayı silmez, taşımaz. Aynı klip iki kere yüklenmez.

MENÜ (pencerenin üstünde)
- Program: duraklat/devam, "Windows açılınca otomatik başlat" (aç/kapat), masaüstü ve Başlat menüsü kısayolu,
  program / ayar klasörü, günlük, "Programı kaldır".
- Yedek: klasörleri şimdi tara, Telegram'daki yedekleri doğrula, kayıtları Telegram'dan yeniden kur,
  arşiv grubunu Telegram'da aç.
- Hesap: hangi Telegram hesabı, çıkış yap.   Yardım: güncellemeleri kontrol et, sürüm notları, hakkında.

AYARLAR NEREDE?
- Ayarlar, Telegram girişi ve yedek kayıtları %APPDATA%\\KlipKalkani klasöründe durur (Program > Ayar klasörünü aç).
  Yeni sürümü başka bir klasöre açsan da ayarların gelir, kurulum tekrar sorulmaz.
- Eski bir sürümü (1.6 ve öncesi) güncelliyorsan: yeni zip'i eski klasörün üstüne açman yeter; ayarlar ilk açılışta
  kendiliğinden oraya taşınır. Başka klasöre açtıysan kurulum ekranında "Ayarlarımı buraya al"a bas.

GÜNCELLEME
- Program kendini GitHub'dan otomatik günceller; bir şey yapmana gerek yok. Sürüm pencerenin başlığında yazar.
- Hemen bakmak istersen: Yardım > "Güncellemeleri kontrol et" (ya da Ayarlar sekmesindeki düğme).

DİĞER
- Ayarlar sekmesi: hangi Telegram hesabına yedeklendiğini gösterir. "Çıkış yap" bu bilgisayardaki girişi kapatır;
  yedekler Telegram'da kalır, aynı hesapla tekrar girince kaldığı yerden sürer.
- Telegram, hesap başına indirmeyi ~6 MB/sn ile sınırlar; toplu geri yükleme biraz sürer.
- Ayar klasöründeki klip_kalkani.session dosyası Telegram hesabına erişim demektir, KİMSEYLE PAYLAŞMA.
"""


def copy_python(dst):
    src = sys.base_prefix
    os.makedirs(dst)
    for name in os.listdir(src):
        if name in SKIP_TOP:
            continue
        s = os.path.join(src, name)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dst, name), ignore=NO_CACHE)
        else:
            shutil.copy2(s, dst)
    lib_src, lib_dst = os.path.join(src, 'Lib'), os.path.join(dst, 'Lib')
    os.makedirs(lib_dst)
    for name in os.listdir(lib_src):
        if name in SKIP_LIB:
            continue
        s = os.path.join(lib_src, name)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(lib_dst, name), ignore=NO_CACHE)
        else:
            shutil.copy2(s, lib_dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api', help='API bilgisi JSON dosyası (api_id, api_hash): pakete gömülür')
    ap.add_argument('--cikti', default=os.path.join(HERE, 'dist'))
    args = ap.parse_args()
    if sys.version_info[:2] != (3, 12):
        print('Uyarı: paket Python 3.12 ile denendi.')
    sys.path.insert(0, HERE)
    import klip_kalkani as kk

    work = os.path.join(args.cikti, 'KlipKalkani')
    if os.path.exists(work):
        shutil.rmtree(work)  # önceki derlemenin çalışma klasörü
    print('Python kopyalanıyor…')
    copy_python(os.path.join(work, 'python'))
    site = os.path.join(work, 'python', 'Lib', 'site-packages')
    print('Kütüphaneler kuruluyor…')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', site, '-r',
                    os.path.join(HERE, 'gereksinimler.txt'), '--no-warn-script-location', '-q'], check=True)
    shutil.rmtree(os.path.join(site, 'bin'), ignore_errors=True)  # pip'in yazdığı, yolu gömülü exe'ler
    for f in APP_FILES:
        shutil.copy2(os.path.join(HERE, f), work)
    with open(os.path.join(work, 'BENİ OKU.txt'), 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write(README)
    suffix = ''
    if args.api:
        with open(args.api, encoding='utf-8') as f:
            api = json.load(f)
        if not (str(api.get('api_id', '')).isdigit() and len(api.get('api_hash', '')) == 32):
            sys.exit('API dosyası geçersiz: {"api_id": 123, "api_hash": "32 karakter"}')
        with open(os.path.join(work, 'varsayilan.json'), 'w', encoding='utf-8') as f:
            json.dump({'api_id': int(api['api_id']), 'api_hash': api['api_hash']}, f, indent=2)
        suffix = '-api'
    for dp, dns, fns in os.walk(work):
        for d in list(dns):
            if d == '__pycache__':
                shutil.rmtree(os.path.join(dp, d), ignore_errors=True)
    out = os.path.join(args.cikti, f'KlipKalkani-{kk.VERSION}{suffix}.zip')
    tmp = out + '.yeni'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dp, dns, fns in os.walk(work):
            for fn in fns:
                p = os.path.join(dp, fn)
                z.write(p, os.path.join('KlipKalkani', os.path.relpath(p, work)))
    with zipfile.ZipFile(tmp) as z:
        if z.testzip() is not None:
            sys.exit('Zip bozuk çıktı')
    os.replace(tmp, out)
    print(f'Hazır: {out} ({os.path.getsize(out) / 1e6:.1f} MB)')


if __name__ == '__main__':
    main()
