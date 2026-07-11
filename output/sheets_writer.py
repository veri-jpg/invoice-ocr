import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import gspread
from pydantic import BaseModel

from config import SheetsConfig
from core.schemas import ExtractionResult

logger = logging.getLogger(__name__)

HEADERS = [
    "Timestamp",
    "Sumber",
    "Pengirim",
    "Nama File",
    "No Invoice",
    "Supplier",
    "Tgl Invoice",
    "Jatuh Tempo",
    "DPP",
    "PPN",
    "Total",
    "No Faktur Pajak",
    "Status Ekstraksi",
    "Issues",
    "Status Bayar",
]

WRITABLE_STATUSES = {"auto_ok", "perlu_review"}


class SheetsWriteError(Exception):
    """Dilempar kalau panggilan ke Google Sheets API gagal."""


class WriteOutcome(BaseModel):
    status: Literal["ditulis", "duplikat", "duplikat_beda_total", "dilewati", "gagal_tulis"]
    nomor_invoice: str | None = None
    nama_supplier: str | None = None
    total_lama: Decimal | None = None
    total_baru: Decimal | None = None
    error: str | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _dup_key(nomor_invoice: str | None, nama_supplier: str | None) -> tuple[str, str] | None:
    if not nomor_invoice or not nama_supplier:
        return None
    return (_normalize(nomor_invoice), _normalize(nama_supplier))


def _resolve_source(source_info: dict) -> tuple[str, str]:
    """Turunkan (sumber, pengirim) dari source_info yang dibentuk tiap channel.
    channels/email_poller.py dan channels/telegram_bot.py mengisi "type"; kalau
    tidak ada (mis. dipanggil dari run.py/run_batch.py) dianggap "cli"."""
    tipe = source_info.get("type", "cli")
    if tipe == "email":
        return "email", source_info.get("sender") or "-"
    if tipe == "telegram":
        username = source_info.get("username")
        chat_id = source_info.get("chat_id")
        pengirim = username or (f"id:{chat_id}" if chat_id is not None else "-")
        return "telegram", pengirim
    return tipe, source_info.get("pengirim", "-")


def _to_number(value: Decimal | None) -> float | str:
    return float(value) if value is not None else ""


def _to_date_str(value) -> str:
    return value.isoformat() if value is not None else ""


def _build_row(result: ExtractionResult, timestamp: str) -> list:
    data = result.data
    sumber, pengirim = _resolve_source(result.source_info)
    return [
        timestamp,
        sumber,
        pengirim,
        result.source_file,
        data.nomor_invoice or "",
        data.nama_supplier or "",
        _to_date_str(data.tanggal_invoice),
        _to_date_str(data.tanggal_jatuh_tempo),
        _to_number(data.dpp),
        _to_number(data.ppn),
        _to_number(data.total),
        data.nomor_faktur_pajak or "",
        result.status,
        "; ".join(result.issues),
        "",
    ]


def _ensure_header(worksheet: gspread.Worksheet) -> None:
    if not worksheet.row_values(1):
        worksheet.append_row(HEADERS, value_input_option="RAW")


def _parse_cached_total(raw) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _load_cache(worksheet: gspread.Worksheet) -> dict[tuple[str, str], Decimal | None]:
    cache: dict[tuple[str, str], Decimal | None] = {}
    for row in worksheet.get_all_records():
        key = _dup_key(str(row.get("No Invoice") or "") or None, str(row.get("Supplier") or "") or None)
        if key is None:
            continue
        cache[key] = _parse_cached_total(row.get("Total"))
    return cache


def connect_worksheet(config: SheetsConfig) -> gspread.Worksheet:
    try:
        client = gspread.service_account(filename=config.service_account_file)
        spreadsheet = client.open_by_key(config.spreadsheet_id)
        try:
            worksheet = spreadsheet.worksheet(config.worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=config.worksheet_name, rows=1000, cols=len(HEADERS)
            )
        _ensure_header(worksheet)
        return worksheet
    except Exception as e:
        raise SheetsWriteError(f"Gagal terhubung ke Google Sheets: {e}") from e


class SheetsWriter:
    def __init__(self, config: SheetsConfig):
        self._worksheet = connect_worksheet(config)
        self._cache = _load_cache(self._worksheet)
        logger.info(
            "Worksheet '%s' siap, %d baris duplikat-cache dimuat",
            config.worksheet_name,
            len(self._cache),
        )

    def append_result(self, result: ExtractionResult) -> WriteOutcome:
        if result.status not in WRITABLE_STATUSES:
            return WriteOutcome(status="dilewati")

        data = result.data
        key = _dup_key(data.nomor_invoice, data.nama_supplier)

        if key is not None and key in self._cache:
            cached_total = self._cache[key]
            if cached_total == data.total:
                return WriteOutcome(
                    status="duplikat",
                    nomor_invoice=data.nomor_invoice,
                    nama_supplier=data.nama_supplier,
                )
            return WriteOutcome(
                status="duplikat_beda_total",
                nomor_invoice=data.nomor_invoice,
                nama_supplier=data.nama_supplier,
                total_lama=cached_total,
                total_baru=data.total,
            )

        timestamp = datetime.now(timezone.utc).isoformat()
        row = _build_row(result, timestamp)
        try:
            self._worksheet.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            raise SheetsWriteError(f"Gagal menulis baris ke Google Sheets: {e}") from e

        if key is not None:
            self._cache[key] = data.total

        logger.info("Baris ditulis: %s / %s", data.nomor_invoice, data.nama_supplier)
        return WriteOutcome(
            status="ditulis",
            nomor_invoice=data.nomor_invoice,
            nama_supplier=data.nama_supplier,
        )
