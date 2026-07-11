from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class InvoiceData(BaseModel):
    is_invoice: bool | None = None
    jenis_dokumen: (
        Literal["invoice", "surat_jalan", "penawaran", "faktur_pajak", "lainnya"] | None
    ) = None
    nomor_invoice: str | None = None
    nama_supplier: str | None = None
    tanggal_invoice: date | None = None
    tanggal_jatuh_tempo: date | None = None
    dpp: Decimal | None = None
    ppn: Decimal | None = None
    total: Decimal | None = None
    nomor_faktur_pajak: str | None = None


class ExtractionResult(BaseModel):
    data: InvoiceData
    status: Literal["auto_ok", "perlu_review", "bukan_invoice", "gagal_proses"]
    issues: list[str]
    source_file: str
    source_info: dict = {}
