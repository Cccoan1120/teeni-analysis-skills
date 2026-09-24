from __future__ import annotations

import codecs
from pathlib import Path


def detect_csv_encoding(path: str | Path) -> str:
    source = Path(path)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV encoding must be UTF-8 or GB18030")
