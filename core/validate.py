from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from core.schemas import InvoiceData

AMOUNT_TOLERANCE = Decimal("100")
PPN_RATE_TOLERANCE = Decimal("0.01")
PPN_RATES = (Decimal("0.11"), Decimal("0.12"))
MAX_INVOICE_AGE_DAYS = 365 * 2


def validate(
    data: InvoiceData,
) -> tuple[Literal["auto_ok", "perlu_review", "bukan_invoice"], list[str]]:
    # Rule 0: bukan invoice -> skip semua validasi lain
    if data.is_invoice is False:
        return "bukan_invoice", [f"Dokumen terdeteksi sebagai: {data.jenis_dokumen}"]

    issues: list[str] = []

    # Rule 5: required fields
    if data.nomor_invoice is None:
        issues.append("nomor_invoice tidak ditemukan (field wajib)")
    if data.nama_supplier is None:
        issues.append("nama_supplier tidak ditemukan (field wajib)")
    if data.total is None:
        issues.append("total tidak ditemukan (field wajib)")

    # Rule 1: dpp + ppn ≈ total
    if data.dpp is not None and data.ppn is not None and data.total is not None:
        selisih = abs((data.dpp + data.ppn) - data.total)
        if selisih > AMOUNT_TOLERANCE:
            issues.append(
                f"dpp + ppn ({data.dpp + data.ppn}) tidak sama dengan total ({data.total}), "
                f"selisih Rp{selisih}"
            )

    # Rule 2: ppn ≈ 11% or 12% of dpp
    if data.dpp is not None and data.ppn is not None and data.dpp != 0:
        actual_rate = data.ppn / data.dpp
        if not any(abs(actual_rate - rate) <= PPN_RATE_TOLERANCE for rate in PPN_RATES):
            issues.append(
                f"ppn ({data.ppn}) bukan 11% atau 12% dari dpp ({data.dpp}), "
                f"rasio aktual {actual_rate:.2%}"
            )

    # Rule 3: tanggal_jatuh_tempo >= tanggal_invoice
    if data.tanggal_jatuh_tempo is not None and data.tanggal_invoice is not None:
        if data.tanggal_jatuh_tempo < data.tanggal_invoice:
            issues.append(
                f"tanggal_jatuh_tempo ({data.tanggal_jatuh_tempo}) lebih awal dari "
                f"tanggal_invoice ({data.tanggal_invoice})"
            )

    # Rule 4: tanggal_invoice not more than 2 years ago, not in the future
    if data.tanggal_invoice is not None:
        today = date.today()
        if data.tanggal_invoice > today:
            issues.append(f"tanggal_invoice ({data.tanggal_invoice}) ada di masa depan")
        elif today - data.tanggal_invoice > timedelta(days=MAX_INVOICE_AGE_DAYS):
            issues.append(
                f"tanggal_invoice ({data.tanggal_invoice}) lebih dari 2 tahun lalu"
            )

    status: Literal["auto_ok", "perlu_review"] = "perlu_review" if issues else "auto_ok"
    return status, issues
