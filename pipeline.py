import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import load_sheets_config
from core.extract import ExtractionAPIError, extract_invoice
from core.preprocess import preprocess_file
from core.schemas import ExtractionResult, InvoiceData
from core.validate import validate
from output.sheets_writer import SheetsWriter, WriteOutcome

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    extraction: ExtractionResult
    write_outcome: WriteOutcome


_writer: SheetsWriter | None = None
_writer_init_failed = False


def _get_writer() -> SheetsWriter | None:
    """Lazy singleton: koneksi ke Google Sheets dan cache duplikat dibuat sekali
    per proses. Kalau init gagal (config kosong / API error), tidak dicoba lagi
    supaya tiap dokumen tidak menunggu timeout koneksi berulang-ulang."""
    global _writer, _writer_init_failed
    if _writer is not None:
        return _writer
    if _writer_init_failed:
        return None
    try:
        _writer = SheetsWriter(load_sheets_config())
        logger.info("Terhubung ke Google Sheets")
        return _writer
    except Exception as e:
        logger.error("Gagal terhubung ke Google Sheets: %s", e)
        _writer_init_failed = True
        return None


def _write_to_sheets(result: ExtractionResult) -> WriteOutcome:
    writer = _get_writer()
    if writer is None:
        return WriteOutcome(status="gagal_tulis", error="Google Sheets tidak dapat diakses")
    try:
        return writer.append_result(result)
    except Exception as e:
        logger.error("Gagal menulis ke Google Sheets: %s", e)
        return WriteOutcome(status="gagal_tulis", error=str(e))


def process_document(file_bytes: bytes, filename: str, source_info: dict) -> ProcessResult:
    """Orkestrator satu pintu untuk semua channel (CLI, email, telegram):
    preprocess -> extract -> validate -> tulis ke Google Sheets. Channel tidak
    boleh memanggil core/ atau output/ langsung."""
    try:
        extraction = _run_pipeline(file_bytes, filename, source_info)
    except Exception as e:
        logger.exception("Error tak terduga saat memproses %s", filename)
        extraction = ExtractionResult(
            data=InvoiceData(),
            status="gagal_proses",
            issues=[f"Error tak terduga: {e}"],
            source_file=filename,
            source_info=source_info,
        )

    write_outcome = _write_to_sheets(extraction)
    return ProcessResult(extraction=extraction, write_outcome=write_outcome)


def _run_pipeline(file_bytes: bytes, filename: str, source_info: dict) -> ExtractionResult:
    suffix = Path(filename).suffix.lower()

    try:
        image_bytes, media_type, page_count = _preprocess_bytes(file_bytes, suffix)
    except (FileNotFoundError, ValueError) as e:
        return ExtractionResult(
            data=InvoiceData(),
            status="gagal_proses",
            issues=[str(e)],
            source_file=filename,
            source_info=source_info,
        )

    notes: list[str] = []
    if page_count > 1:
        notes.append(f"PDF {page_count} halaman, hanya halaman 1 yang diproses")

    try:
        raw_data = extract_invoice(image_bytes, media_type)
    except ExtractionAPIError as e:
        return ExtractionResult(
            data=InvoiceData(),
            status="perlu_review",
            issues=notes + [f"API error: {e}"],
            source_file=filename,
            source_info=source_info,
        )
    except ValueError as e:
        return ExtractionResult(
            data=InvoiceData(),
            status="perlu_review",
            issues=notes + [f"Gagal parsing respons API: {e}"],
            source_file=filename,
            source_info=source_info,
        )

    try:
        invoice_data = InvoiceData(**raw_data)
    except Exception as e:
        return ExtractionResult(
            data=InvoiceData(),
            status="perlu_review",
            issues=notes + [f"Data hasil ekstraksi tidak valid: {e}"],
            source_file=filename,
            source_info=source_info,
        )

    status, issues = validate(invoice_data)
    all_issues = notes + issues
    if notes and status == "auto_ok":
        status = "perlu_review"

    return ExtractionResult(
        data=invoice_data,
        status=status,
        issues=all_issues,
        source_file=filename,
        source_info=source_info,
    )


def _preprocess_bytes(file_bytes: bytes, suffix: str) -> tuple[bytes, str, int]:
    """core.preprocess.preprocess_file expects a path on disk; ini menulis bytes
    ke file sementara supaya core/ tidak perlu diubah untuk menerima bytes."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        return preprocess_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
