// Klip Kalkanı.exe: yanındaki taşınabilir Python'la (python\pythonw.exe) ya da geliştirme kopyasında .venv ile
// arayuz.pyw'yi açar ve kapanır; konsol penceresi açılmaz. Verilen argümanlar pencereye aynen geçer.
// Derlemek için: python exe_yap.py  (Windows'la gelen .NET Framework derleyicisi csc.exe kullanılır)
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("Klip Kalkanı")]
[assembly: AssemblyProduct("Klip Kalkanı")]
[assembly: AssemblyDescription("Oyun kliplerini Telegram'a yedekler")]
[assembly: AssemblyCopyright("github.com/GroundPower/klip-kalkani")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Baslatici
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string[] pythons = {
            Path.Combine(dir, @"python\pythonw.exe"),         // taşınabilir paket
            Path.Combine(dir, @".venv\Scripts\pythonw.exe"),  // geliştirme kopyası
        };
        string py = null;
        foreach (string p in pythons)
        {
            if (File.Exists(p)) { py = p; break; }
        }
        string script = Path.Combine(dir, "arayuz.pyw");
        if (py == null || !File.Exists(script))
        {
            Hata((py == null ? "\"python\" klasörü" : "arayuz.pyw") + " bulunamadı.\n\n" +
                 "Zip'i klasörüyle birlikte tamamen çıkardığından emin ol.");
            return 1;
        }
        var cmd = new StringBuilder("-E -s ").Append(Tirnak(script));
        foreach (string a in args) cmd.Append(' ').Append(Tirnak(a));
        var psi = new ProcessStartInfo(py, cmd.ToString());
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            Hata(e.Message);
            return 1;
        }
        return 0;
    }

    static void Hata(string text)
    {
        MessageBoxW(IntPtr.Zero, "Klip Kalkanı başlatılamadı: " + text, "Klip Kalkanı", 0x10);
    }

    // Windows komut satırı kuralıyla tırnaklama: Python argümanı aynen geri alır (boşluk, tırnak, ters bölü).
    static string Tirnak(string s)
    {
        if (s.Length > 0 && s.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return s;
        var sb = new StringBuilder("\"");
        int bs = 0;
        foreach (char c in s)
        {
            if (c == '\\') { bs++; continue; }
            if (c == '"') sb.Append('\\', bs * 2 + 1).Append('"');
            else sb.Append('\\', bs).Append(c);
            bs = 0;
        }
        sb.Append('\\', bs * 2).Append('"');
        return sb.ToString();
    }
}
