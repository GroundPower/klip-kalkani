# -*- coding: utf-8 -*-
"""Yeni sürüm yayınlamadan önce surum.json'u üretir (dosya özetleri + sürüm).

Kurulu programlar GitHub'daki surum.json'a bakar; sürüm numarası kendilerininkinden büyükse
listelenen dosyaları v<sürüm> etiketinden indirir, özetlerini doğrular ve kendilerini günceller.

Adımlar:
  1. klip_kalkani.py içindeki VERSION'ı artır.
  2. python yayinla.py "değişiklik notu"
  3. git add -A && git commit -m "Sürüm X" && git tag vX && git push && git push --tags
"""
import datetime as dt
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import klip_kalkani as kk  # noqa: E402

files = {}
for name in kk.GUNCELLENEN_DOSYALAR:
    with open(os.path.join(HERE, name), 'rb') as f:
        files[name] = hashlib.sha256(f.read()).hexdigest()
manifest = {
    'surum': kk.VERSION,
    'etiket': f'v{kk.VERSION}',
    'tarih': dt.date.today().isoformat(),
    'notlar': ' '.join(sys.argv[1:]),
    'dosyalar': files,
    'gereken_moduller': ['telethon', 'cryptg', 'psutil', 'av', 'PIL', 'sv_ttk', 'sounddevice'],
}
with open(os.path.join(HERE, 'surum.json'), 'w', encoding='utf-8') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(json.dumps(manifest, ensure_ascii=False, indent=2))
