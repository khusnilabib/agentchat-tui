# AgentChat TUI

TUI chat untuk sistem AI agent berbasis Python standard library. Mendukung endpoint OpenAI-compatible, tool calling, dan tiga tool sandboxed: `list_files`, `read_file`, dan `run_command`.

## Jalankan

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-4o-mini"              # opsional
export OPENAI_BASE_URL="https://api.openai.com/v1" # opsional
python3 agentchat_tui.py
```

Untuk provider kompatibel OpenAI, ubah `OPENAI_BASE_URL` dan model sesuai provider.

## Kontrol

- `Enter` kirim pesan
- `Ctrl+C` / `Ctrl+Q` keluar
- `Ctrl+L` bersihkan percakapan
- `Ctrl+S` muat ulang konfigurasi dari environment
- `↑` / `↓` scroll percakapan

## Catatan keamanan

API key hanya dibaca dari environment dan tidak ditulis ke disk. Tool dibatasi ke direktori proyek saat ini. Perintah yang terlihat destruktif (mis. `rm`, `sudo`, `shutdown`) ditolak, tetapi tetap review perintah yang dijalankan agent sebelum memakai proyek ini di lingkungan sensitif.

## Lisensi

MIT
