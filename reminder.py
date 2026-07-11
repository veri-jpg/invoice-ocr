import asyncio
import logging
import sys
from datetime import date, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from telegram import Bot

from config import ReminderConfig, load_reminder_config, load_sheets_config, load_telegram_config
from logging_config import setup_logging
from output.sheets_writer import connect_worksheet

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_jatuh_tempo(raw) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_total(raw) -> str:
    if isinstance(raw, (int, float)):
        return f"Rp{raw:,.0f}".replace(",", ".")
    return str(raw) if raw not in (None, "") else "-"


def _collect_due_items(rows: list[dict], reminder_days: int) -> tuple[list[dict], list[dict]]:
    today = date.today()
    overdue: list[dict] = []
    upcoming: list[dict] = []

    for row in rows:
        jatuh_tempo = _parse_jatuh_tempo(row.get("Jatuh Tempo"))
        if jatuh_tempo is None:
            continue

        status_bayar = str(row.get("Status Bayar") or "").strip().lower()
        if status_bayar == "lunas":
            continue

        selisih = (jatuh_tempo - today).days
        if selisih > reminder_days:
            continue

        item = {
            "supplier": row.get("Supplier") or "-",
            "no_invoice": row.get("No Invoice") or "-",
            "total": _format_total(row.get("Total")),
            "selisih": selisih,
        }
        (overdue if selisih < 0 else upcoming).append(item)

    overdue.sort(key=lambda x: x["selisih"])
    upcoming.sort(key=lambda x: x["selisih"])
    return overdue, upcoming


def _format_item(item: dict) -> str:
    selisih = item["selisih"]
    if selisih < 0:
        jatuh_tempo_text = f"terlambat {abs(selisih)} hari"
    elif selisih == 0:
        jatuh_tempo_text = "jatuh tempo hari ini"
    else:
        jatuh_tempo_text = f"{selisih} hari lagi"
    return f"{item['supplier']} — {item['no_invoice']} — {item['total']} — {jatuh_tempo_text}"


def _format_message(overdue: list[dict], upcoming: list[dict]) -> str:
    lines: list[str] = []
    if overdue:
        lines.append(f"OVERDUE ({len(overdue)}):")
        lines.extend(_format_item(item) for item in overdue)
    if upcoming:
        if lines:
            lines.append("")
        lines.append(f"SEGERA JATUH TEMPO ({len(upcoming)}):")
        lines.extend(_format_item(item) for item in upcoming)
    return "\n".join(lines)


def run_reminder_check() -> None:
    # Dipanggil APScheduler tiap hari (job terpisah dari loop utama) — kalau
    # sampai raise, tangkap & log di sini sendiri supaya jelas di log file,
    # bukan cuma numpuk di traceback default APScheduler. Scheduler tetap
    # jalan buat jadwal berikutnya walau satu run gagal.
    try:
        sheets_cfg = load_sheets_config()
        reminder_cfg = load_reminder_config()
        telegram_cfg = load_telegram_config()

        worksheet = connect_worksheet(sheets_cfg)
        rows = worksheet.get_all_records()

        overdue, upcoming = _collect_due_items(rows, reminder_cfg.days)
        if not overdue and not upcoming:
            logger.info("Tidak ada tagihan jatuh tempo/overdue, tidak mengirim reminder.")
            return

        message = _format_message(overdue, upcoming)
        asyncio.run(
            Bot(telegram_cfg.bot_token).send_message(chat_id=reminder_cfg.chat_id, text=message)
        )
        logger.info("Reminder terkirim: %d overdue, %d segera jatuh tempo.", len(overdue), len(upcoming))
    except Exception:
        logger.exception("Gagal menjalankan pengecekan reminder")


def _run_scheduler(reminder_cfg: ReminderConfig) -> None:
    scheduler = BlockingScheduler()
    scheduler.add_job(run_reminder_check, "cron", hour=reminder_cfg.hour, minute=0)
    logger.info("Reminder scheduler dimulai, jalan tiap hari jam %02d:00...", reminder_cfg.hour)
    scheduler.start()


def main() -> None:
    setup_logging()
    if "--once" in sys.argv:
        run_reminder_check()
        return

    reminder_cfg = load_reminder_config()
    _run_scheduler(reminder_cfg)


if __name__ == "__main__":
    main()
