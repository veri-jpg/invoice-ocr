import base64
import json
import logging
import os
import re
import time

import anthropic

logger = logging.getLogger(__name__)

# Nama model bisa di-override lewat env var ANTHROPIC_MODEL / GEMINI_MODEL (lihat .env.example)
# tanpa perlu edit kode. Dibaca saat runtime (bukan konstanta di sini) karena load_dotenv()
# baru dipanggil setelah modul ini di-import.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

MAX_RETRYABLE_ATTEMPTS = 4  # 1 percobaan awal + 3 retry
BACKOFF_BASE_SECONDS = 1.0
JSON_RETRY_ATTEMPTS = 3  # total percobaan kalau respons gagal di-parse sebagai JSON

# Status code yang aman untuk di-retry: rate limit (429) dan server overload
# (529 di Anthropic, 503 "UNAVAILABLE" di Gemini).
RETRYABLE_STATUS_CODES = {429, 503, 529}

EXTRACTION_PROMPT = """Ekstrak data dari invoice Indonesia ini. Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown.

Pertama, tentukan jenis dokumen ini:
- is_invoice: true jika ini invoice/tagihan/nota penjualan, false jika bukan
- jenis_dokumen: "invoice", "surat_jalan", "penawaran", "faktur_pajak", atau "lainnya"
Jika bukan invoice, tetap isi is_invoice dan jenis_dokumen, dan isi semua field lain dengan null.

Field yang diambil:
- nomor_invoice: nomor invoice/nota
- nama_supplier: nama perusahaan penerbit invoice
- tanggal_invoice: format YYYY-MM-DD
- tanggal_jatuh_tempo: format YYYY-MM-DD (cari kata "jatuh tempo", "due date", atau "TOP/term of payment" — jika tertulis TOP 30 hari, hitung dari tanggal invoice)
- dpp: dasar pengenaan pajak, angka tanpa pemisah ribuan
- ppn: nilai PPN, angka tanpa pemisah ribuan
- total: nilai total tagihan, angka tanpa pemisah ribuan
- nomor_faktur_pajak: nomor seri faktur pajak jika ada (format XXX.XXX-XX.XXXXXXXX)

Aturan:
- Jika field tidak ditemukan atau tidak terbaca jelas, isi null. JANGAN menebak.
- Angka dalam Rupiah: "15.400.000" berarti 15400000. Field dpp/ppn/total HARUS berupa
  angka JSON yang valid (contoh: 15400000) — JANGAN PERNAH menyertakan titik atau koma
  sebagai pemisah ribuan dalam angka JSON, itu membuat JSON tidak valid.
- Jangan tertukar antara tanggal invoice dan tanggal jatuh tempo
"""


class ExtractionAPIError(Exception):
    """Dilempar jika panggilan ke API vision LLM (provider mana pun) gagal."""


class RetryableAPIError(ExtractionAPIError):
    """Rate limit (429) atau server overload (503/529) — aman untuk di-retry dengan backoff."""


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


# Model kadang lupa instruksi prompt dan nulis dpp/ppn/total Rupiah dengan titik
# pemisah ribuan asli (mis. "total": 15.400.000), yang bikin JSON invalid (dua
# titik dalam satu angka). Beda dari desimal asli (mis. 1169318.58, cuma 1 titik),
# jadi aman ditarget spesifik ke 3 field numerik ini saja.
_THOUSANDS_SEPARATOR_RE = re.compile(
    r'("(?:dpp|ppn|total)"\s*:\s*)(\d{1,3}(?:\.\d{3}){2,})(?=\s*[,}\]])'
)


def _repair_thousands_separators(text: str) -> str:
    return _THOUSANDS_SEPARATOR_RE.sub(lambda m: m.group(1) + m.group(2).replace(".", ""), text)


def _best_effort_parse(text: str) -> dict:
    """Vision LLM kadang keluarin JSON yang hampir bener tapi ada glitch kecil.
    Dua yang pernah kejadian: (1) dpp/ppn/total pakai titik ribuan asli, (2) ada
    kurung/teks nyisa setelah objek JSON yang sebenarnya sudah lengkap & valid.
    Coba beberapa cara parse berurutan sebelum benar-benar nyerah (masih 1 respons
    API yang sama, bukan panggilan API baru)."""
    last_error: json.JSONDecodeError | None = None
    for candidate in (text, _repair_thousands_separators(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
        try:
            # raw_decode ambil objek JSON valid pertama, abaikan sisa teks
            # di belakangnya (kasus "Extra data" kalau model dobel penutup).
            obj, _end_pos = json.JSONDecoder().raw_decode(candidate.strip())
            return obj
        except json.JSONDecodeError as e:
            last_error = e
    raise last_error


def _call_anthropic(image_bytes: bytes, media_type: str) -> str:
    try:
        client = anthropic.Anthropic()
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )
        text_parts = [block.text for block in response.content if block.type == "text"]
        return "".join(text_parts)
    except (anthropic.RateLimitError, anthropic.OverloadedError) as e:
        raise RetryableAPIError(f"Anthropic API error ({e.status_code}): {e}") from e
    except anthropic.APIError as e:
        raise ExtractionAPIError(f"Anthropic API error: {e}") from e


def _call_gemini(image_bytes: bytes, media_type: str) -> str:
    from google.genai import errors as genai_errors

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=media_type),
                EXTRACTION_PROMPT,
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

        # Diagnostik: kalau respons kepotong/rusak, ini yang jawab PASTI kenapa
        # (MAX_TOKENS, SAFETY, dll) — bukan nebak dari pola teks doang.
        finish_reason = response.candidates[0].finish_reason if response.candidates else None
        if finish_reason is not None and finish_reason != types.FinishReason.STOP:
            logger.warning(
                "Gemini finish_reason=%s (bukan STOP), usage_metadata=%s",
                finish_reason,
                response.usage_metadata,
            )

        return response.text or ""
    except genai_errors.APIError as e:
        if e.code in RETRYABLE_STATUS_CODES:
            raise RetryableAPIError(f"Gemini API error ({e.code}): {e}") from e
        raise ExtractionAPIError(f"Gemini API error: {e}") from e
    except Exception as e:
        raise ExtractionAPIError(f"Gemini API error: {e}") from e


def extract_invoice(image_bytes: bytes, media_type: str) -> dict:
    """Call the vision API (Anthropic by default, or Gemini if LLM_PROVIDER=gemini) and
    return the parsed JSON dict.

    Rate limit (429) dan server overload (503/529) di-retry dengan exponential
    backoff sampai MAX_RETRYABLE_ATTEMPTS. Kegagalan parsing JSON di-retry
    terpisah sampai JSON_RETRY_ATTEMPTS (bukan masalah rate limit, jadi tanpa delay)."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    call_fn = _call_gemini if provider == "gemini" else _call_anthropic

    last_json_error: Exception | None = None
    json_attempts = 0
    for retryable_attempt in range(MAX_RETRYABLE_ATTEMPTS):
        try:
            raw_text = call_fn(image_bytes, media_type)
        except RetryableAPIError as e:
            if retryable_attempt == MAX_RETRYABLE_ATTEMPTS - 1:
                raise ExtractionAPIError(
                    f"Gagal setelah {MAX_RETRYABLE_ATTEMPTS} percobaan (rate limit/overload): {e}"
                ) from e
            time.sleep(BACKOFF_BASE_SECONDS * (2**retryable_attempt))
            continue

        json_attempts += 1
        cleaned = _strip_markdown_fences(raw_text)
        try:
            return _best_effort_parse(cleaned)
        except json.JSONDecodeError as e:
            last_json_error = e
            if json_attempts >= JSON_RETRY_ATTEMPTS:
                logger.warning("Respons API gagal di-parse sebagai JSON, raw text: %s", raw_text[:1000])
                raise ValueError(
                    f"Gagal parsing JSON dari respons API setelah {json_attempts} percobaan: {last_json_error}"
                )
            continue

    raise ValueError(f"Gagal parsing JSON dari respons API: {last_json_error}")
