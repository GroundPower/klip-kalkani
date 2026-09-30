# 🛡 Klip Kalkanı

Oyun kliplerini (Medal, NVIDIA, Outplayed, OBS, Xbox Game Bar…) **Telegram'da sadece senin göreceğin gizli bir gruba** otomatik yedekleyen Windows programı. Disk bozulsa da klipler kaybolmaz.

## Neler yapar

- **Otomatik yedek:** Klip klasörlerini kendisi bulur, yeni klipleri arka planda yükler. Bilgisayar her açıldığında kendiliğinden çalışır.
- **Saatin yanında simge:** Arka plan çalışırken bildirim alanında Klip Kalkanı simgesi durur; üstüne gelince ne yaptığını söyler (yükleniyor %45, her şey yedekli, oyun açık, bekliyor…). Tıklayınca pencere açılır; sağ tıkta duraklat, şimdi tara ve **Çıkış**.
- **Kapatınca sorar:** Pencere kapatılırken yedekleme arka planda sürsün mü, tamamen mi kapansın diye sorar ("bir daha sorma" seçilebilir). Tamamen kapatılan yedekleme pencere açılınca ya da bilgisayar yeniden açılınca kaldığı yerden sürer. Pencere kapanınca programın kendisi de kesin kapanır, arkada Python kalmaz.
- **Takılırsa söyler:** Durum sekmesi arka planın hangi adımda olduğunu yazar (bağlanıyor, klasörler taranıyor, klip hazırlanıyor…). "Devam et" arka planı gerçekten başlatır: takılı kalanı kapatır, bozulan otomatik başlatmayı onarır; yine açılmazsa sebebini ekranda yazar.
- **Orijinal kalite:** Dosyalar yeniden sıkıştırılmaz. Telegram'da önizlemeli video olarak durur, telefondan izlenir. 2 GB'tan büyük dosyalar (Premium'da 4 GB) parçalı yüklenir, geri indirilirken birleştirilir.
- **Her oyun ayrı konuda:** Arşiv, forum modundaki bir grup. League of Legends, Valorant, Apex… her birinin kendi konusu var.
- **Oyunu bozmaz:** Oyun açıkken yükleme durur. Gündüz ve gece için ayrı hız sınırı konabilir.
- **Bu klip bitince duraklat:** Durum sekmesindeki düğme (menüde ve saatin yanındaki simgede de var): yüklenen klip biter, sıradakine geçilmeden yedekleme duraklar. "Devam et" ile kaldığı yerden sürer.
- **Kopyaları ayıklar:** Aynı klip farklı klasörlerde olsa da bir kere yüklenir.
- **Geri yükleme:** Oyuna, aya ya da isme göre seçip indirir. Her parça SHA-256 ile orijinaline karşı doğrulanır.
- **Düzenle:** Sesli önizleme (60 FPS / tam FPS), zaman çubuğu, A–B seçimi, **kayıpsız kesme** (saniyeler sürer), **tam kare kesme** (NVIDIA NVENC) ve **kayıpsız birleştirme**.
- **Ses kanalları:** Medal'in ayrı kaydettiği kanallar (tüm ses, oyun, Discord, mikrofon) için aç/kapat, seviye (%0–200), tek dinle ve seviye göstergesi; oynatırken değiştirmek anında duyulur. Kaydederken ses olduğu gibi kopyalanır ya da ayarlanan seviyelerle **tek kanalda birleştirilir** (Discord'da ve telefonda her şey duyulur) / ayrı kanallar olarak kaydedilir.
- **Kendini günceller:** Yeni sürümler bu repodan otomatik iner. Dosyalar özetleriyle doğrulanır, eski sürüm `eski_surum/` klasöründe yedeklenir. Ayarlar sekmesindeki "Güncellemeleri kontrol et" ile elle de bakılır.
- **Hesap ve çıkış:** Ayarlar sekmesi yedeklerin hangi Telegram hesabına gittiğini gösterir. "Çıkış yap" bu bilgisayardaki girişi kapatır (Telegram'ın cihaz listesinden de düşer); yedekler Telegram'da kalır. Aynı hesapla tekrar girince kaldığı yerden sürer. Başka bir hesapla girilirse o hesapta yeni bir arşiv grubu açılır.
- **Menü:** Koyu temaya uyan, ikonlu Program / Yedek / Hesap / Yardım menüsü ve kısayol tuşları (Ctrl+D duraklat, F5 tara, Ctrl+U güncelleme, F1 yardım, Ctrl+Q tamamen kapat). Otomatik başlatmayı aç/kapat, kısayollar, şimdi tara, yedekleri doğrula, kayıtları Telegram'dan yeniden kur, geri yükle, programı kaldır.
- **Gelişmiş ayarlar:** Sağ üstteki düğmeden bütün ayarlar tek yerde: hız planının saatleri ve limitleri, oyun listesi, tarama aralığı, uzantılar, oyun adı düzeltmeleri, ses kanalı adları, otomatik güncelleme…
- **Telegram'sız kullanım:** Kurulumda ya da giriş ekranında "Telegram olmadan devam et": düzenleyici, klasörler ve oyun özetleri çalışır, yedekleme kapalı kalır. İstendiğinde Hesap > Telegram'a bağlan.
- **Ayarlar kaybolmaz:** Ayarlar, Telegram girişi ve yedek kayıtları `%APPDATA%\KlipKalkani`'de durur. Yeni sürüm başka klasöre açılsa da ayarlar gelir. 1.6 ve önceki sürümlerin program klasöründeki ayarları ilk açılışta oraya taşınır.
- **Hiçbir şey silmez:** Ne bilgisayardaki dosyalara ne de Telegram'daki yedeklere dokunur.

## Kurulum

1. [Releases](../../releases) sayfasından `KlipKalkani-<sürüm>.zip`'i indir. Kalıcı bir klasöre çıkar (ör. `C:\KlipKalkani`).
2. `Klip Kalkanı.exe`'ye çift tıkla. Kurulum penceresi seni adım adım götürür:
   - Klip klasörleri
   - Hız ayarı
   - Telegram API bilgisi (bir kere): [my.telegram.org](https://my.telegram.org/apps) → *API development tools*
   - Telefonla Telegram girişi

Python kurmana gerek yok. Paket kendi taşınabilir Python'uyla gelir.

## Gizlilik

- Klipler sadece senin Telegram hesabındaki gizli gruba gider. Başka bir sunucu yok.
- Telegram oturumu (`klip_kalkani.session`), ayarlar ve veritabanı sadece senin bilgisayarında (`%APPDATA%\KlipKalkani`) durur. Repoya hiçbir zaman girmez (`.gitignore` yalnızca program dosyalarına izin verir).
- İnternet hızı ölçümü için Cloudflare'in hız testi adresine rastgele veri gönderilir. Kişisel veri gitmez.

## Geliştirici notları

| Dosya | Görevi |
|---|---|
| `klip_kalkani.py` | Yedekleme motoru ve komut satırı: `calis`, `tara`, `durum`, `geri-yukle`, `dogrula`, `indeks-yenile`, `cikis`, `kaldir` |
| `arayuz.pyw` | Pencere: kurulum, durum, hız, oyunlar, klasörler, geri yükleme, ayarlar (hesap, çıkış, güncelleme) |
| `duzenle.py` | Önizleme, kesme ve birleştirme (PyAV) |
| `baslatici.cs` | `Klip Kalkanı.exe`: yanındaki taşınabilir Python'la (ya da `.venv` ile) pencereyi açan küçük başlatıcı |
| `exe_yap.py` | `baslatici.cs`'i Windows'la gelen `csc.exe` ile derler (sadece başlatıcı değişince) |
| `paket_yap.py` | Taşınabilir zip'i yapar: `python paket_yap.py [--api varsayilan.json]` |
| `yayinla.py` | Yeni sürüm için `surum.json`'u üretir |

Yeni sürüm yayınlamak için:

1. `VERSION`'ı artır.
2. `python yayinla.py "not"` çalıştır.
3. Commit at, `v<sürüm>` etiketini ekle, push et.
4. İstersen `paket_yap.py` ile yaptığın zip'i Releases'e ekle.
