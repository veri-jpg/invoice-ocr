import logging
import time
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import load_telegram_config
from core.schemas import ExtractionResult
from logging_config import setup_logging
from output.sheets_writer import WriteOutcome
from pipeline import process_document

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 15

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_IMAGE_SIZE_BYTES = 50 * 1024
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024

INSTRUKSI = (
    "Kirim invoice sebagai foto atau dokumen (PDF/JPG/PNG/WEBP) untuk diekstrak datanya."
)
PESAN_TIDAK_DIIZINKAN = "Maaf, chat ini tidak diizinkan menggunakan bot ini."


def _is_allowed(update: Update, allowed_chat_ids: set[int]) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id in allowed_chat_ids


def _format_result(result: ExtractionResult) -> str:
    data = result.data
    if result.status == "auto_ok":
        return (
            "Invoice berhasil diproses:\n"
            f"Supplier: {data.nama_supplier}\n"
            f"No. Invoice: {data.nomor_invoice}\n"
            f"Total: {data.total}\n"
            f"Jatuh tempo: {data.tanggal_jatuh_tempo}"
        )
    if result.status == "perlu_review":
        issues_text = "\n".join(f"- {issue}" for issue in result.issues)
        return (
            "Perlu dicek manual:\n"
            f"Supplier: {data.nama_supplier}\n"
            f"No. Invoice: {data.nomor_invoice}\n"
            f"Total: {data.total}\n"
            f"Jatuh tempo: {data.tanggal_jatuh_tempo}\n\n"
            f"Masalah:\n{issues_text}"
        )
    if result.status == "bukan_invoice":
        return f"Dokumen ini terdeteksi sebagai {data.jenis_dokumen}, bukan invoice."
    issues_text = "; ".join(result.issues) if result.issues else "kesalahan tidak diketahui"
    return f"Gagal memproses dokumen: {issues_text}"


def _format_write_outcome(outcome: WriteOutcome) -> str | None:
    if outcome.status == "ditulis":
        return "Tercatat di rekap."
    if outcome.status == "duplikat":
        return f"Invoice {outcome.nomor_invoice} dari {outcome.nama_supplier} sudah ada di rekap, dilewati."
    if outcome.status == "duplikat_beda_total":
        return (
            f"PERHATIAN: invoice {outcome.nomor_invoice} dari {outcome.nama_supplier} sudah ada "
            f"dengan total {outcome.total_lama}, dokumen baru totalnya {outcome.total_baru}. "
            "Tidak ditulis otomatis, cek manual."
        )
    if outcome.status == "gagal_tulis":
        return "Ekstraksi berhasil tapi gagal menulis ke rekap, cek log."
    return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_ids = context.bot_data["allowed_chat_ids"]
    if not _is_allowed(update, allowed_chat_ids):
        await update.message.reply_text(PESAN_TIDAK_DIIZINKAN)
        return
    await update.message.reply_text(INSTRUKSI)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_ids = context.bot_data["allowed_chat_ids"]
    if not _is_allowed(update, allowed_chat_ids):
        await update.message.reply_text(PESAN_TIDAK_DIIZINKAN)
        return

    message = update.message

    if message.photo:
        tg_file_ref = message.photo[-1]
        filename = f"{tg_file_ref.file_unique_id}.jpg"
    elif message.document:
        doc = message.document
        ext = Path(doc.file_name or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            await message.reply_text(f"Ekstensi '{ext or '(tidak diketahui)'}' tidak didukung.")
            return
        tg_file_ref = doc
        filename = doc.file_name or f"{doc.file_unique_id}{ext}"
    else:
        return

    try:
        tg_file = await tg_file_ref.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except TelegramError as e:
        logger.error("Gagal download file dari Telegram: %s", e)
        await message.reply_text(
            "Gagal mengambil file dari Telegram (koneksi bermasalah), coba kirim ulang."
        )
        return

    ext = Path(filename).suffix.lower()
    size = len(file_bytes)
    if size > MAX_FILE_SIZE_BYTES:
        await message.reply_text("File terlalu besar (maksimal 20MB).")
        return
    if ext in IMAGE_EXTENSIONS and size < MIN_IMAGE_SIZE_BYTES:
        await message.reply_text("Gambar terlalu kecil/resolusi rendah untuk diproses.")
        return

    source_info = {
        "type": "telegram",
        "chat_id": update.effective_chat.id if update.effective_chat else None,
        "username": update.effective_user.username if update.effective_user else None,
    }

    await message.reply_text("Memproses dokumen...")
    result = process_document(file_bytes, filename, source_info)
    reply = _format_result(result.extraction)
    outcome_text = _format_write_outcome(result.write_outcome)
    if outcome_text:
        reply += f"\n\n{outcome_text}"
    await message.reply_text(reply)


def _build_application(cfg) -> Application:
    application = Application.builder().token(cfg.bot_token).build()
    application.bot_data["allowed_chat_ids"] = cfg.allowed_chat_ids
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def main() -> None:
    setup_logging()
    cfg = load_telegram_config()

    # run_polling() bisa exit kalau koneksi ke Telegram putus total saat bootstrap
    # (lihat CATATAN_TESTING.md #5). bootstrap_retries=-1 bikin PTB sendiri retry
    # tanpa batas; loop luar ini jaring pengaman kedua kalau tetap ada yang lolos
    # (mis. error lain yang bikin run_polling() return/raise).
    while True:
        try:
            application = _build_application(cfg)
            logger.info("Telegram bot dimulai...")
            application.run_polling(bootstrap_retries=-1, drop_pending_updates=False)
            logger.info("Telegram bot berhenti (shutdown normal).")
            break
        except Exception as e:
            logger.error(
                "Telegram bot berhenti karena error, reconnect dalam %ds: %s",
                RECONNECT_DELAY_SECONDS,
                e,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    main()
