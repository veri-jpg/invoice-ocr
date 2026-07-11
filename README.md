# invoice-ocr

Ekstraksi otomatis data invoice (foto/PDF) pakai vision LLM (Anthropic Claude atau Google Gemini), masuk lewat Telegram bot atau email, hasilnya divalidasi lalu dicatat ke Google Sheets. Ada juga reminder harian buat tagihan yang mau/sudah jatuh tempo.

## Alur kerja

```
                    ┌── channels/telegram_bot.py ──┐
                    │                               │
file masuk ─────────┤                               ├──> pipeline.py ──> output/sheets_writer.py ──> Google Sheets
                    │                               │         │
                    └── channels/email_poller.py ───┘         │
                                                                ▼
                                                          core/preprocess.py (JPG/PNG/WEBP/PDF -> gambar siap kirim)
                                                          core/extract.py    (panggil vision LLM, hasil JSON)
                                                          core/validate.py   (cek kewajaran angka & tanggal)

reminder.py (dijadwalkan harian / manual --once) ──> baca semua baris di Google Sheets ──> kirim rangkuman jatuh tempo ke Telegram
```

Semua channel (Telegram, email, CLI) masuk lewat satu pintu: `pipeline.process_document()`. Channel tidak boleh memanggil `core/` atau `output/` langsung.

## Struktur folder

```
invoice-ocr/
├── core/                   # inti: preprocess, extract (vision LLM), validate, schemas — jangan diubah sembarangan
│   ├── preprocess.py       # load JPG/PNG/WEBP/PDF -> JPEG siap kirim ke API (resize, ambil halaman 1 kalau PDF)
│   ├── extract.py          # panggil Anthropic/Gemini vision API, retry otomatis kalau rate limit/overload
│   ├── validate.py         # cek dpp+ppn≈total, rate PPN 11%/12%, tanggal masuk akal, field wajib
│   └── schemas.py          # InvoiceData, ExtractionResult (pydantic)
├── channels/               # cara file/dokumen masuk ke sistem
│   ├── telegram_bot.py     # bot Telegram, terima foto/dokumen, balas hasil + status pencatatan
│   └── email_poller.py     # polling IMAP, proses attachment dari email belum dibaca
├── output/
│   └── sheets_writer.py    # tulis hasil ke Google Sheets, cek duplikat (nomor_invoice + supplier)
├── pipeline.py             # orkestrator: preprocess -> extract -> validate -> tulis ke Sheets
├── reminder.py             # cek jatuh tempo harian, kirim rangkuman ke Telegram
├── config.py               # baca & validasi semua env var
├── logging_config.py       # setup logging terpusat (file + stdout), dipakai semua entry point
├── run.py                  # CLI: proses satu file lewat terminal
├── run_batch.py            # CLI: proses semua file di satu folder sekaligus, cetak ringkasan
├── samples/                # contoh invoice buat testing
├── results/                # output JSON dari run_batch.py (di-gitignore)
├── processed/              # state email_poller.py: id email yang sudah diproses + log (di-gitignore)
├── logs/                   # log runtime semua proses, rotasi otomatis (di-gitignore)
└── service-account.json    # kredensial Google service account (JANGAN commit, sudah di-gitignore)
```

## Persiapan

### 1. Install dependency

```bash
pip install -r requirements.txt
```

`requirements.txt` sudah divalidasi jalan dari nol di venv baru (bukan cuma "kebetulan jalan" di environment dev) — install fresh, lalu semua entry point (`run.py`, `channels.telegram_bot`, `channels.email_poller`, `reminder.py`) berhasil di-import dan dijalankan tanpa `ModuleNotFoundError`. Kalau nambah dependency baru, ulangi tes ini sebelum deploy: `python -m venv /tmp/venv_test && /tmp/venv_test/bin/pip install -r requirements.txt && /tmp/venv_test/bin/python run.py samples/invoice_1.jpg`.

### 2. Isi file `.env`

Salin `.env.example` jadi `.env`, lalu isi semua yang wajib. Rincian per bagian:

#### Ekstraksi (wajib salah satu provider)

| Variable | Wajib? | Keterangan |
|---|---|---|
| `ANTHROPIC_API_KEY` | wajib kalau pakai Anthropic (default) | API key dari console.anthropic.com |
| `LLM_PROVIDER` | opsional | `gemini` untuk pakai Google Gemini (ada free tier) sebagai ganti Anthropic. Default: `anthropic` |
| `GEMINI_API_KEY` | wajib kalau `LLM_PROVIDER=gemini` | API key dari aistudio.google.com |
| `ANTHROPIC_MODEL` / `GEMINI_MODEL` | opsional | Override nama model. Default: `claude-haiku-4-5` / `gemini-3.5-flash` |

#### Channel: Email (`channels/email_poller.py`)

| Variable | Wajib? | Keterangan |
|---|---|---|
| `IMAP_HOST` | wajib | mis. `imap.gmail.com` |
| `IMAP_USER` | wajib | alamat email |
| `IMAP_PASSWORD` | wajib | App Password (bukan password akun biasa — untuk Gmail, buat di myaccount.google.com/apppasswords) |
| `POLL_INTERVAL` | opsional | detik antar polling. Default: `300` |

#### Channel: Telegram (`channels/telegram_bot.py`)

| Variable | Wajib? | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | wajib | dari [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | wajib | daftar chat_id yang **boleh mengirim file ke bot**, dipisah koma |

#### Output: Google Sheets (`output/sheets_writer.py`)

| Variable | Wajib? | Keterangan |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | wajib | path ke file JSON service account Google |
| `SPREADSHEET_ID` | wajib | ID spreadsheet (dari URL, antara `/d/` dan `/edit`) |
| `WORKSHEET_NAME` | opsional | nama tab tujuan. Default: `Invoices` — kalau belum ada, dibuat otomatis |

Langkah setup service account & share spreadsheet ada di bagian [Setup Google Sheets](#setup-google-sheets) di bawah.

#### Reminder (`reminder.py`)

| Variable | Wajib? | Keterangan |
|---|---|---|
| `REMINDER_CHAT_ID` | wajib | chat_id Telegram **tujuan pengiriman** reminder & alert duplikat (beda dari `TELEGRAM_ALLOWED_CHAT_IDS` — ini yang **menerima**, bukan yang boleh mengirim) |
| `REMINDER_DAYS` | opsional | kirim reminder untuk tagihan jatuh tempo ≤ N hari lagi (termasuk yang sudah overdue). Default: `3` |
| `REMINDER_HOUR` | opsional | jam pengiriman reminder harian (0-23, waktu lokal server). Default: `8` |

Startup akan gagal dengan pesan jelas kalau ada env var wajib yang kosong.

## Setup Google Sheets

1. Buka [Google Cloud Console](https://console.cloud.google.com/) → buat/pilih project
2. Aktifkan **Google Sheets API** dan **Google Drive API** (search di search bar → Enable)
3. **IAM & Admin → Service Accounts → Create Service Account** → isi nama → Create and Continue → skip role → Done
4. Buka service account itu → tab **Keys → Add Key → Create new key → JSON** → file otomatis ke-download
5. Taruh file JSON itu di folder project ini, sesuaikan nama file dengan `GOOGLE_SERVICE_ACCOUNT_FILE` di `.env`
6. Buka file JSON, salin nilai `client_email` (formatnya `xxx@xxx.iam.gserviceaccount.com`)
7. Buka Google Spreadsheet tujuan → **Share** → paste email tadi → beri akses **Editor**
8. Ambil `SPREADSHEET_ID` dari URL: `https://docs.google.com/spreadsheets/d/`**`ID_NYA_DI_SINI`**`/edit`

Kalau lupa langkah 7 (share ke service account), nanti kena error `PERMISSION_DENIED` waktu proses dokumen pertama.

## Menjalankan

> Channel harus dijalankan sebagai **module** dari root project (bukan `python channels/xxx.py`), karena butuh import `config`, `core`, `output` dari root:

```bash
# proses satu file lewat terminal
python run.py samples/invoice_1.jpg

# proses semua file di satu folder, hasil JSON masuk ke results/
python run_batch.py samples/

# jalankan bot Telegram (terus jalan, polling)
python -m channels.telegram_bot

# jalankan email poller (terus jalan, polling tiap POLL_INTERVAL detik)
python -m channels.email_poller

# jalankan reminder sekali saja (buat testing / dipanggil dari cron)
python reminder.py --once

# jalankan reminder sebagai scheduler harian (terus jalan, kirim tiap jam REMINDER_HOUR)
python reminder.py
```

`telegram_bot.py`, `email_poller.py`, dan `reminder.py` (mode scheduler) adalah proses yang **terus berjalan** — jalankan masing-masing di terminal/proses terpisah (atau service/systemd/Task Scheduler kalau mau produksi).

## Logging

Semua proses (channel, reminder, CLI) pakai modul `logging` standar Python lewat `logging_config.setup_logging()`, bukan `print()`. Tiap log punya timestamp, level, dan nama modul asalnya.

- Tertulis ke **`logs/invoice-ocr.log`** (rotasi otomatis: max 5MB per file, disimpan 3 file terakhir — aman dibiarkan nyala terus-menerus di VPS tanpa membengkak)
- Sekaligus ke **stdout**, jadi tetap ketangkep kalau proses dijalankan lewat systemd/journalctl, screen, tmux, atau `nohup ... &`
- Level default `INFO`. Set `LOG_LEVEL=DEBUG` di `.env` untuk lihat detail tiap dokumen (payload ekstraksi penuh, dll)

Output CLI (`run.py`, `run_batch.py`) tetap murni lewat `print()` ke stdout (itu memang kontrak output programnya, dibaca skrip lain), terpisah dari log operasional yang jalan di stderr+file — jadi `python run.py file.jpg > hasil.json` tetap aman, tidak kecampur log.

## Ketahanan koneksi (Telegram timeout, IMAP throttle)

Dua proses long-running ini dirancang supaya **tidak exit** kalau koneksi ke server luar putus sesaat — cuma log error lalu reconnect otomatis:

- **`channels/email_poller.py`**: loop utama (`run_poller`) sudah bungkus tiap siklus polling dengan try/except; kalau IMAP gagal/timeout/throttle, error di-log dan proses tidur `POLL_INTERVAL` detik lalu coba reconnect — tidak pernah exit karena error jaringan.
- **`channels/telegram_bot.py`**: `run_polling()` dipanggil dengan `bootstrap_retries=-1` (PTB retry koneksi awal tanpa batas), dibungkus loop luar juga — kalau tetap ada yang lolos jadi exception, di-log dan proses coba bangun ulang bot dari awal setelah jeda 15 detik. Kegagalan download file dari Telegram di tengah proses satu dokumen juga ditangkap terpisah: user dapat balasan "coba kirim ulang" alih-alih bot diam/hang.

Kedua pola ini muncul dari isu nyata yang ditemukan saat testing (lihat `CATATAN_TESTING.md` #5 dan #7) — jaringan ke Telegram/Gmail memang sesekali putus, jadi resilience ini bukan sekadar antisipasi teoretis.

## Status ekstraksi

Tiap dokumen yang diproses dapat salah satu status:

| Status | Arti | Ditulis ke Sheets? |
|---|---|---|
| `auto_ok` | semua field wajib ada, angka & tanggal masuk akal | ya |
| `perlu_review` | ada yang janggal (field kosong, ppn tidak 11%/12%, tanggal aneh, dll) — lihat `issues` | ya |
| `bukan_invoice` | dokumen terdeteksi bukan invoice (surat jalan, penawaran, dll) | tidak |
| `gagal_proses` | error teknis (file rusak, API gagal total, dll) | tidak |

## Deteksi duplikat

Kunci duplikat: **(nomor_invoice, nama_supplier)**, dibandingkan case-insensitive & spasi dirapikan.

| Kondisi | Outcome |
|---|---|
| Nomor invoice & supplier sama, total sama | `duplikat` — tidak ditulis |
| Nomor invoice & supplier sama, total beda | `duplikat_beda_total` — tidak ditulis otomatis, kemungkinan invoice revisi, alert dikirim ke Telegram (`REMINDER_CHAT_ID`) kalau sumbernya email |
| Nomor invoice beda (supplier boleh sama) | ditulis normal |
| Supplier beda (nomor invoice boleh sama) | ditulis normal — penomoran invoice bersifat internal tiap perusahaan |
| Nomor invoice tidak terbaca (null) | ditulis normal — tidak bisa dicek duplikatnya |

## Kolom Google Sheets

`Timestamp | Sumber | Pengirim | Nama File | No Invoice | Supplier | Tgl Invoice | Jatuh Tempo | DPP | PPN | Total | No Faktur Pajak | Status Ekstraksi | Issues | Status Bayar`

Kolom **Status Bayar** sengaja dikosongkan sistem — diisi manual. Isi `lunas` di kolom ini supaya baris itu berhenti muncul di reminder.

## Troubleshooting cepat

| Gejala | Kemungkinan penyebab |
|---|---|
| `ModuleNotFoundError: No module named 'config'` | jalankan pakai `python -m channels.xxx`, bukan `python channels/xxx.py` |
| Data tidak muncul di Sheets padahal `write_outcome: ditulis` | cek nama tab — kalau `WORKSHEET_NAME` belum ada sebelumnya, dibuat tab baru, bisa kelewat kalau cuma lihat `Sheet1` |
| `PERMISSION_DENIED` ke Google Sheets | spreadsheet belum di-share (Editor) ke email service account |
| Email test "tidak pernah keproses" | jangan buka/preview email itu di Gmail sebelum poller sempat jalan — kebuka otomatis mark "Seen", dan poller cuma proses email `unseen` |
| Reminder tidak kekirim padahal ada invoice baru masuk | itu memang bukan reaktif — `reminder.py` cuma kirim kalau dijalankan (terjadwal/`--once`), bukan tiap ada invoice baru |
| Mau lihat log lebih detail per dokumen | set `LOG_LEVEL=DEBUG` di `.env`, lihat `logs/invoice-ocr.log` |
| Proses jalan tapi kelihatannya "diam" lama | itu wajar untuk `email_poller.py` kalau inbox punya banyak email `unseen` lama — dia proses satu-satu dari yang paling lama, cek `logs/invoice-ocr.log` buat lihat progress-nya |

Detail lengkap error yang pernah ditemui saat testing ada di [`CATATAN_TESTING.md`](./CATATAN_TESTING.md).

## Batasan yang perlu diketahui

- Google Sheets: maksimal 10 juta cell per file (gabungan semua tab) — jauh dari kebutuhan pemakaian normal
- Google Sheets API quota: default ~60 write request/menit per project — aman untuk volume dokumen harian biasa, tapi rawan kalau ada lonjakan puluhan dokumen sekaligus (ditangani: gagal tulis tidak bikin crash, statusnya jadi `gagal_tulis`)
- PDF multi-halaman: hanya halaman pertama yang diproses
- Ukuran file: gambar minimal 50KB (di bawah itu dianggap resolusi kurang), maksimal 20MB
