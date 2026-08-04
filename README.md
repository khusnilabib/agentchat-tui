# Ciprocode CLI

Terminal AI agent client dengan tampilan modern bergaya agent CLI, workspace rail, conversation viewport, dan composer yang jelas.

Premium, dependency-free Python TUI untuk sistem chat AI agent dengan brand **Ciprocode CLI**. Layout utama terdiri dari sidebar workspace/session, header model dan koneksi, area percakapan terpisah berdasarkan pengirim, serta composer di bagian bawah. Dibuat untuk endpoint OpenAI Chat Completions dan provider OpenAI-compatible.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)

## Fitur

- Streaming respons token-by-token dengan fallback otomatis ke non-streaming.
- Agent loop dengan tool calling dan batas langkah (`AGENT_MAX_STEPS`).
- Tool built-in: `list_files`, `read_file`, `run_command`.
- Workspace sandbox: file dibatasi ke direktori proyek saat ini.
- Approval gate untuk setiap tool (`y` izinkan, `n` tolak).
- Safety policy untuk menolak command destruktif umum.
- Retry otomatis untuk error 408/429/5xx.
- Session persistence SQLite di `~/.agentchat/sessions.db`.
- Export transkrip ke Markdown.
- Dukungan provider, model, temperature, system prompt, dan stream melalui environment.
- UI keyboard-first yang ringan; tidak membutuhkan pip dependency.

## Instalasi dan menjalankan

```bash
python3 --version  # 3.10+
pip install -r requirements.txt
export OPENAI_API_KEY="your-key"
python3 agentchat_tui.py
```

Di Windows CMD, gunakan `set` untuk sesi aktif:

```bat
py -m pip install -r requirements.txt
set OPENAI_API_KEY=your-key
py agentchat_tui.py
```

Di Windows, `windows-curses` adalah dependency yang dibutuhkan agar TUI `curses` dapat berjalan. SDK DashScope bersifat opsional karena aplikasi memakai endpoint OpenAI-compatible secara langsung; jika diperlukan untuk program Python lain, gunakan `pip install -r requirements-optional.txt`.

Provider alternatif, termasuk Qwen:

```bash
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
export DASHSCOPE_MODEL="qwen3.7-max"
python3 agentchat_tui.py
```

Aplikasi otomatis memilih endpoint DashScope ketika model diawali `qwen` dan `OPENAI_BASE_URL` tidak diatur. Payload Qwen juga mengaktifkan `enable_thinking=true`. Template konfigurasi tersedia di `.env.example`; salin menjadi `.env` dan isi key Anda:

Di Linux/macOS:

```bash
cp .env.example .env
chmod 600 .env
```

Di Windows Command Prompt (CMD):

```bat
copy .env.example .env
notepad .env
```

Di Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

`chmod` tidak tersedia di CMD/PowerShell dan tidak diperlukan untuk aplikasi ini. `.env` sudah masuk `.gitignore` dan tidak boleh di-commit.

Environment lengkap:

| Variable | Default | Keterangan |
|---|---|---|
| `OPENAI_API_KEY` | — | API key OpenAI |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL OpenAI-compatible |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model chat |
| `DASHSCOPE_API_KEY` | — | API key Alibaba Model Studio / DashScope |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | Endpoint Qwen international |
| `DASHSCOPE_MODEL` | `qwen3.7-max` | Preset model Qwen dengan deep thinking aktif |
| `OPENAI_TEMPERATURE` | `0.2` | Kreativitas respons |
| `AGENT_MAX_STEPS` | `8` | Batas putaran tool per prompt |
| `AGENT_STREAM` | `1` | Set `0` untuk mematikan streaming |
| `AGENT_APPROVE_TOOLS` | `1` | Set `0` untuk auto-approve tool |
| `AGENT_SYSTEM_PROMPT` | built-in | System prompt kustom |

## Perintah di dalam TUI

- `/help` — tampilkan bantuan
- `/clear` — mulai percakapan baru
- `/save [judul]` — simpan sesi ke SQLite
- `/sessions` — daftar sesi tersimpan
- `/load ID` — buka sesi berdasarkan ID
- `/model NAME` — ganti model aktif
- `/provider URL` — ganti base URL provider
- `/approve on|off` — ubah approval tool
- `/export FILE.md` — ekspor percakapan ke Markdown
- `/retry` — ulangi prompt terakhir
- `/quit` — simpan dan keluar

Shortcut: `Enter` kirim, `Ctrl+L` clear, `Ctrl+S` reload environment, `↑/↓` scroll, `Ctrl+C` atau `Ctrl+Q` keluar.

## Keamanan

API key hanya dibaca dari environment. Jangan menaruh token di source code, `.env` yang ter-commit, atau command history. Tool `run_command` tetap harus direview; safety filter bukan pengganti sandbox OS penuh. Untuk lingkungan produksi, jalankan dalam container atau akun dengan permission minimal.

## Lisensi

MIT
