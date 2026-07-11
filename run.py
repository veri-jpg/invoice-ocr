import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from logging_config import setup_logging
from pipeline import ProcessResult, process_document

load_dotenv()


def process_file(path: str | Path) -> ProcessResult:
    path = Path(path)
    return process_document(path.read_bytes(), path.name, source_info={})


def main() -> None:
    setup_logging()
    if len(sys.argv) != 2:
        print("Penggunaan: python run.py path/ke/file.jpg", file=sys.stderr)
        sys.exit(1)

    file_path = Path(sys.argv[1])
    if not file_path.exists():
        print(f"Error: file tidak ditemukan: {file_path}", file=sys.stderr)
        sys.exit(1)

    result = process_file(file_path)
    output = {
        "extraction": result.extraction.model_dump(),
        "write_outcome": result.write_outcome.model_dump(),
    }
    print(json.dumps(output, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
