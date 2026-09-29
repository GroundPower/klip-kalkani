# -*- coding: utf-8 -*-
"""Klip Kalkanı.exe başlatıcısını baslatici.cs'ten derler (Windows'la gelen .NET Framework derleyicisi csc.exe).

Sadece baslatici.cs değişince çalıştır: her derleme farklı bayt üretir, kurulu programlar da exe'yi yeniden indirir.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'Klip Kalkanı.exe')

windir = os.environ.get('WINDIR', r'C:\Windows')
found = sorted(glob.glob(os.path.join(windir, r'Microsoft.NET\Framework64\v4.*\csc.exe'))
               or glob.glob(os.path.join(windir, r'Microsoft.NET\Framework\v4.*\csc.exe')))
if not found:
    sys.exit('csc.exe bulunamadı (.NET Framework 4 gerekli)')
tmp = os.path.join(HERE, 'baslatici.tmp.exe')  # derleyiciye ASCII ad; sonra asıl adına taşınır
r = subprocess.run([found[-1], '/nologo', '/codepage:65001', '/target:winexe', '/optimize+', '/platform:anycpu',
                    '/win32icon:' + os.path.join(HERE, 'kalkan.ico'), '/out:' + tmp, os.path.join(HERE, 'baslatici.cs')],
                   capture_output=True, text=True, encoding='mbcs', errors='replace')
if r.returncode != 0:
    sys.exit('Derlenemedi:\n' + (r.stdout + r.stderr).strip())
os.replace(tmp, OUT)
print(f'Hazır: {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)')
