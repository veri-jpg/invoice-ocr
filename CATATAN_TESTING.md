# Catatan Testing — Output Layer (Google Sheets + Reminder)

Tanggal: 2026-07-11

> **Update (kesiapan VPS, sesi yang sama):** poin #4 (buffering) dan #5/#7 (Telegram timeout & IMAP throttle) di bawah ini sudah dibereskan — semua `print()` diganti `logging` (auto-flush, tidak perlu `-u` lagi), dan `telegram_bot.py`/`email_poller.py` sekarang auto-reconnect kalau koneksi putus (lihat bagian "Ketahanan koneksi" di `README.md`). Catatan di bawah dibiarkan apa adanya sebagai riwayat penyebabnya.

## Yang dibangun

- `output/sheets_writer.py` — tulis hasil ekstraksi ke Google Sheets, dengan cek duplikat (nomor_invoice + supplier)
- `reminder.py` — cek jatuh tempo harian, kirim rangkuman ke Telegram
- `pipeline.py`, `config.py`, `channels/telegram_bot.py`, `channels/email_poller.py` — diintegrasikan supaya tiap invoice yang diproses otomatis tercatat ke Sheets

## Hasil akhir — semua terverifikasi jalan

| Bagian | Status |
|---|---|
| Tulis ke Google Sheets (auto-create header + tab) | ✅ |
| Deteksi duplikat (sama persis / beda total / beda nomor / beda supplier) | ✅ |
| Telegram bot: terima file → balas ke user → tercatat di Sheets | ✅ |
| Email poller: terima attachment → tercatat di Sheets | ✅ |
| Notifikasi `duplikat_beda_total` dari email ke Telegram | belum sempat dites kasusnya, tapi kode sama pola dengan yang lain |
| `reminder.py --once` → kirim rangkuman OVERDUE ke Telegram | ✅ |

## Error yang ditemui selama testing, dan pelajarannya

### 1. `ModuleNotFoundError: No module named 'config'`
**Penyebab:** jalanin `python channels/telegram_bot.py` langsung — working dir Python jadi folder `channels/`, bukan root project, jadi `config.py` di root tidak ketemu.
**Fix:** jalankan sebagai module dari root: `python -m channels.telegram_bot` (bukan `python channels/telegram_bot.py`). Sama untuk `email_poller.py`.

### 2. `SPREADSHEET_ID` salah format
**Penyebab:** copas ID dari address bar langsung termasuk `/edit?usp=sharing` di belakangnya.
**Fix:** `SPREADSHEET_ID` cuma bagian ID-nya saja, antara `/d/` dan `/edit` di URL spreadsheet.

### 3. "Kok gak ada data di Sheets?" — padahal ada
**Penyebab:** `WORKSHEET_NAME` default `"Invoices"`. Kalau spreadsheet belum punya tab dengan nama itu, sistem otomatis bikin tab baru — dan defaultnya Google Sheets selalu punya tab `Sheet1` kosong duluan. User cuma lihat `Sheet1` (kosong), gak sadar ada tab baru `Invoices` di sebelahnya.
**Pelajaran:** kalau data "hilang", cek dulu nama tab yang aktif dilihat sebelum curiga ke kode.

### 4. Output proses background gak kelihatan (stdout keburu ketutup buffer)
**Penyebab:** Python default block-buffered stdout kalau outputnya di-redirect ke file (bukan terminal interaktif). Jadi log `print(...)` gak langsung muncul di file output.
**Fix:** jalankan dengan `python -u` (unbuffered) kalau mau lihat log real-time dari proses background.

### 5. Telegram bot timeout (`httpcore.ConnectTimeout`, `TimedOut`)
**Penyebab:** koneksi jaringan sesaat ke `api.telegram.org` dari environment testing gak stabil — kadang gagal total connect (`get_me()` pas startup), kadang cuma gagal download file. Setelah dicoba ulang beberapa kali, jalan normal.
**Pelajaran:** ini bukan bug kode — kalau ada `TimedOut`/`ConnectTimeout` yang sporadis, coba restart dulu sebelum curiga ada yang salah di logic.

### 6. Email invoice test "gak keproses-proses"
**Penyebab #1 — antrean backlog:** inbox aslinya punya >1400 email belum dibaca (promo dll). `email_poller.py` proses SEMUA email `unseen`, dari yang paling lama duluan. Email test baru jadi antre di paling belakang.
**Penyebab #2 — email keburu ke-mark "Seen":** begitu email test dibuka/dipreview manual di aplikasi Gmail buat ngecek "udah masuk belum", Gmail otomatis mark jadi "read". Poller cuma proses yang statusnya `unseen`, jadi email itu otomatis gak akan pernah ke-pick up walau isinya belum pernah diproses sama sekali.
**Pelajaran paling penting:** JANGAN buka/preview email test di Gmail sebelum poller sempat jalan — cukup kirim, lalu tunggu, jangan dicek manual dulu. Kalau butuh vps testing cepat tanpa nunggu antrean/tanpa takut ke-mark-seen, bisa fetch by UID/sender langsung terus panggil `_handle_message()` manual (cara ini yang dipakai buat verifikasi sesi ini).

### 7. IMAP `System Error (Failure)` dari Gmail
**Penyebab:** kemungkinan Gmail throttle/rate-limit sesi IMAP gara-gara proses ribuan fetch+flag operation beruntun dalam waktu singkat (dari poller ngunyah backlog).
**Fix:** stop proses, reconnect ulang (buka koneksi IMAP baru) — biasanya langsung pulih. Kode sendiri sebenarnya sudah auto-retry (nunggu `POLL_INTERVAL` lalu coba lagi), cuma kalau mau cepat bisa restart manual.

### 8. Reminder gak otomatis kekirim tiap ada invoice baru
**Ini bukan error, tapi kesalahpahaman soal desain sistem:**
- Invoice masuk dari Telegram/email → langsung ditulis ke Sheets + balasan ke user. TIDAK otomatis kirim notif ke channel reminder.
- Notif jatuh tempo/overdue ke `REMINDER_CHAT_ID` cuma dikirim oleh `reminder.py` yang jalan terjadwal (harian) atau manual `python reminder.py --once`. Sistemnya scan SEMUA baris di Sheets tiap kali jalan, bukan reaktif per-invoice masuk.
**Pelajaran:** kalau mau lihat efek reminder buat invoice yang baru saja masuk, harus trigger `reminder.py --once` manual (atau tunggu jadwal harian jalan).

## Konfigurasi akhir yang perlu diingat

- `REMINDER_CHAT_ID` beda dari `TELEGRAM_ALLOWED_CHAT_IDS` — yang pertama tujuan bot **mengirim** notifikasi, yang kedua daftar chat yang **boleh pakai** bot.
- `GOOGLE_SERVICE_ACCOUNT_FILE` harus di-share manual (Editor access) ke spreadsheet tujuan via email `client_email` dari file JSON service account — kalau lupa, kena `PERMISSION_DENIED`.
- Kalau mau jalanin channel secara manual: selalu pakai `python -m channels.<nama>`, jangan `python channels/<nama>.py`.
