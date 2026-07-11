import json
import sys
from collections import Counter
from pathlib import Path

from core.schemas import ExtractionResult, InvoiceData
from logging_config import setup_logging
from run import process_file

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
RESULTS_DIR = Path("results")


def find_invoice_files(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def save_result(result: ExtractionResult, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / f"{Path(result.source_file).stem}.json"
    out_path.write_text(
        json.dumps(result.model_dump(), indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary_table(results: list[ExtractionResult]) -> None:
    name_width = max((len(Path(r.source_file).name) for r in results), default=20)
    name_width = max(name_width, 20)

    print(f"\n{'File':<{name_width}}  {'Status':<14}  Issues")
    print("-" * (name_width + 14 + 10))
    for r in results:
        issues_text = "; ".join(r.issues) if r.issues else "-"
        print(f"{Path(r.source_file).name:<{name_width}}  {r.status:<14}  {issues_text}")


def print_field_stats(results: list[ExtractionResult]) -> None:
    field_names = list(type(results[0].data).model_fields.keys()) if results else []
    null_counts: Counter[str] = Counter()
    for r in results:
        for field in field_names:
            if getattr(r.data, field) is None:
                null_counts[field] += 1

    if not null_counts:
        return

    print("\nField yang paling sering null/bermasalah:")
    for field, count in null_counts.most_common():
        print(f"  {field}: {count}/{len(results)} file")


def main() -> None:
    setup_logging()
    if len(sys.argv) != 2:
        print("Penggunaan: python run_batch.py samples/", file=sys.stderr)
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.exists() or not folder.is_dir():
        print(f"Error: folder tidak ditemukan: {folder}", file=sys.stderr)
        sys.exit(1)

    files = find_invoice_files(folder)
    if not files:
        print(f"Tidak ada file JPG/PNG/PDF ditemukan di {folder}")
        return

    results: list[ExtractionResult] = []
    for file_path in files:
        print(f"Memproses {file_path.name}...", file=sys.stderr)
        try:
            result = process_file(file_path).extraction
        except Exception as e:
            # process_file menangkap error yang diketahui dan mengembalikan
            # status gagal_proses; ini jaring pengaman terakhir supaya satu
            # file yang bermasalah tidak menghentikan seluruh batch.
            print(f"  Error tak terduga: {e}", file=sys.stderr)
            result = ExtractionResult(
                data=InvoiceData(),
                status="gagal_proses",
                issues=[f"Error tak terduga: {e}"],
                source_file=str(file_path),
            )

        save_result(result, RESULTS_DIR)
        results.append(result)

    if not results:
        print("Tidak ada file yang berhasil diproses.")
        return

    print_summary_table(results)

    auto_ok_count = sum(1 for r in results if r.status == "auto_ok")
    perlu_review_count = sum(1 for r in results if r.status == "perlu_review")
    gagal_proses_count = sum(1 for r in results if r.status == "gagal_proses")
    print(
        f"\nRingkasan: {auto_ok_count} auto_ok, {perlu_review_count} perlu_review, "
        f"{gagal_proses_count} gagal_proses"
    )

    print_field_stats(results)


if __name__ == "__main__":
    main()
