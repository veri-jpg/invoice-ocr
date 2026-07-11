import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from imap_tools import AND, MailBox, MailMessageFlags
from telegram import Bot

from config import load_email_config, load_reminder_config, load_telegram_config
from logging_config import setup_logging
from output.sheets_writer import WriteOutcome
from pipeline import process_document

logger = logging.getLogger(__name__)

PROCESSED_DIR = Path(__file__).resolve().parent.parent / "processed"
PROCESSED_IDS_FILE = PROCESSED_DIR / "email_processed.txt"
LOG_FILE = PROCESSED_DIR / "email_log.jsonl"

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_IMAGE_SIZE_BYTES = 50 * 1024
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024


def _load_processed_ids() -> set[str]:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    if not PROCESSED_IDS_FILE.exists():
        return set()
    return {
        line.strip()
        for line in PROCESSED_IDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _mark_processed(message_id: str) -> None:
    with PROCESSED_IDS_FILE.open("a", encoding="utf-8") as f:
        f.write(message_id + "\n")


def _log_event(
    message_id: str,
    sender: str,
    subject: str,
    action: str,
    reason: str | None,
    write_outcomes: list[str] | None = None,
) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "action": action,
        "reason": reason,
        "write_outcome": write_outcomes,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _notify_duplikat_beda_total(sender: str, subject: str, outcome: WriteOutcome) -> None:
    """duplikat_beda_total berarti kemungkinan invoice revisi -> perlu perhatian
    manusia segera, jadi dorong juga ke Telegram, bukan cuma tercatat di log."""
    try:
        reminder_cfg = load_reminder_config()
        telegram_cfg = load_telegram_config()
    except RuntimeError as e:
        logger.warning("Gagal mengirim notifikasi duplikat_beda_total (config tidak lengkap): %s", e)
        return

    text = (
        f"PERHATIAN: email dari {sender} (subjek: {subject}) berisi invoice "
        f"{outcome.nomor_invoice} dari {outcome.nama_supplier} yang sudah ada di rekap "
        f"dengan total {outcome.total_lama}, tapi dokumen baru totalnya {outcome.total_baru}. "
        "Tidak ditulis otomatis, cek manual."
    )
    try:
        asyncio.run(Bot(telegram_cfg.bot_token).send_message(chat_id=reminder_cfg.chat_id, text=text))
    except Exception as e:
        logger.error("Gagal mengirim notifikasi Telegram: %s", e)


def _get_message_id(msg) -> str:
    raw = msg.headers.get("message-id")
    if raw:
        return raw[0] if isinstance(raw, tuple) else str(raw)
    return f"uid:{msg.uid}"


def _filter_attachment(att) -> tuple[bool, str | None]:
    """Cek berurutan, berhenti di filter pertama yang gagal."""
    if (att.content_disposition or "").lower() != "attachment":
        return False, "inline_only"

    ext = Path(att.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, "ext_tidak_didukung"

    size = len(att.payload)
    if size > MAX_FILE_SIZE_BYTES:
        return False, "ukuran"
    if ext in IMAGE_EXTENSIONS and size < MIN_IMAGE_SIZE_BYTES:
        return False, "ukuran"

    return True, None


def _handle_message(mailbox: MailBox, msg, processed_ids: set[str]) -> None:
    message_id = _get_message_id(msg)
    sender = msg.from_ or ""
    subject = msg.subject or ""

    if message_id in processed_ids:
        _log_event(message_id, sender, subject, "skipped", "sudah_diproses")
        mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
        return

    attachments = list(msg.attachments)
    if not attachments:
        _log_event(message_id, sender, subject, "skipped", "no_attachment")
        mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
        return

    valid_attachments = []
    last_reject_reason = "no_attachment"
    for att in attachments:
        ok, reason = _filter_attachment(att)
        if ok:
            valid_attachments.append(att)
        else:
            last_reject_reason = reason

    if not valid_attachments:
        _log_event(message_id, sender, subject, "skipped", last_reject_reason)
        mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
        return

    source_info = {"type": "email", "sender": sender, "subject": subject, "message_id": message_id}
    write_outcomes: list[str] = []
    for att in valid_attachments:
        result = process_document(att.payload, att.filename, source_info)
        logger.info(
            "%s (dari %s): status=%s write_outcome=%s",
            att.filename,
            sender,
            result.extraction.status,
            result.write_outcome.status,
        )
        logger.debug(
            "Detail ekstraksi %s: %s",
            att.filename,
            json.dumps(result.extraction.model_dump(), default=str, ensure_ascii=False),
        )
        write_outcomes.append(result.write_outcome.status)
        if result.write_outcome.status == "duplikat_beda_total":
            _notify_duplikat_beda_total(sender, subject, result.write_outcome)

    processed_ids.add(message_id)
    _mark_processed(message_id)
    _log_event(message_id, sender, subject, "processed", None, write_outcomes)
    mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)


def poll_once(mailbox: MailBox, processed_ids: set[str]) -> None:
    for msg in mailbox.fetch(AND(seen=False), mark_seen=False):
        _handle_message(mailbox, msg, processed_ids)


def run_poller() -> None:
    setup_logging()
    cfg = load_email_config()
    processed_ids = _load_processed_ids()

    logger.info("Email poller dimulai (host=%s, interval=%ds)...", cfg.imap_host, cfg.poll_interval)
    while True:
        try:
            with MailBox(cfg.imap_host).login(cfg.imap_user, cfg.imap_password) as mailbox:
                poll_once(mailbox, processed_ids)
        except Exception as e:
            # Ketemu saat testing (CATATAN_TESTING.md #7): IMAP kadang throttle
            # kalau proses banyak email berturut-turut. Tidak fatal — loop ini
            # otomatis reconnect di iterasi berikutnya, proses tidak exit.
            logger.error("Error saat polling email, reconnect setelah %ds: %s", cfg.poll_interval, e)
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    run_poller()
