import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_POLL_INTERVAL_SECONDS = 300


def _get_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Env var wajib '{name}' tidak diset atau kosong. Cek file .env (lihat .env.example)."
        )
    return value


@dataclass
class EmailConfig:
    imap_host: str
    imap_user: str
    imap_password: str
    poll_interval: int


@dataclass
class TelegramConfig:
    bot_token: str
    allowed_chat_ids: set[int]


@dataclass
class SheetsConfig:
    service_account_file: str
    spreadsheet_id: str
    worksheet_name: str


@dataclass
class ReminderConfig:
    days: int
    chat_id: int
    hour: int


def load_email_config() -> EmailConfig:
    poll_interval_raw = os.environ.get("POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_SECONDS)).strip()
    try:
        poll_interval = int(poll_interval_raw)
    except ValueError as e:
        raise RuntimeError(
            f"POLL_INTERVAL harus berupa angka (detik), dapat: '{poll_interval_raw}'"
        ) from e

    return EmailConfig(
        imap_host=_get_required("IMAP_HOST"),
        imap_user=_get_required("IMAP_USER"),
        imap_password=_get_required("IMAP_PASSWORD"),
        poll_interval=poll_interval,
    )


def load_telegram_config() -> TelegramConfig:
    bot_token = _get_required("TELEGRAM_BOT_TOKEN")
    raw_ids = _get_required("TELEGRAM_ALLOWED_CHAT_IDS")

    try:
        allowed_chat_ids = {int(part.strip()) for part in raw_ids.split(",") if part.strip()}
    except ValueError as e:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_CHAT_IDS harus berisi angka dipisah koma, "
            f"contoh: 123456789,987654321. Dapat: '{raw_ids}'"
        ) from e

    if not allowed_chat_ids:
        raise RuntimeError("TELEGRAM_ALLOWED_CHAT_IDS tidak boleh kosong")

    return TelegramConfig(bot_token=bot_token, allowed_chat_ids=allowed_chat_ids)


def load_sheets_config() -> SheetsConfig:
    worksheet_name = os.environ.get("WORKSHEET_NAME", "Invoices").strip() or "Invoices"
    return SheetsConfig(
        service_account_file=_get_required("GOOGLE_SERVICE_ACCOUNT_FILE"),
        spreadsheet_id=_get_required("SPREADSHEET_ID"),
        worksheet_name=worksheet_name,
    )


def load_reminder_config() -> ReminderConfig:
    chat_id_raw = _get_required("REMINDER_CHAT_ID")
    try:
        chat_id = int(chat_id_raw)
    except ValueError as e:
        raise RuntimeError(
            f"REMINDER_CHAT_ID harus berupa angka, dapat: '{chat_id_raw}'"
        ) from e

    days_raw = os.environ.get("REMINDER_DAYS", "3").strip()
    try:
        days = int(days_raw)
    except ValueError as e:
        raise RuntimeError(f"REMINDER_DAYS harus berupa angka, dapat: '{days_raw}'") from e

    hour_raw = os.environ.get("REMINDER_HOUR", "8").strip()
    try:
        hour = int(hour_raw)
    except ValueError as e:
        raise RuntimeError(f"REMINDER_HOUR harus berupa angka (0-23), dapat: '{hour_raw}'") from e
    if not (0 <= hour <= 23):
        raise RuntimeError(f"REMINDER_HOUR harus antara 0-23, dapat: {hour}")

    return ReminderConfig(days=days, chat_id=chat_id, hour=hour)
