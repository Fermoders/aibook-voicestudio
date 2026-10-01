from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from razdel import sentenize


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    pause_ms: int


SUPPORTED_DOCUMENTS = {
    ".txt": "Текст",
    ".fb2": "FB2",
    ".epub": "EPUB",
    ".docx": "Word",
    ".pdf": "PDF",
}


def load_document(path: str | Path) -> str:
    document = Path(path)
    if not document.is_file():
        raise FileNotFoundError(f"Файл не найден: {document}")

    suffix = document.suffix.lower()
    loaders = {
        ".txt": _load_text,
        ".fb2": _load_fb2,
        ".epub": _load_epub,
        ".docx": _load_docx,
        ".pdf": _load_pdf,
    }
    try:
        loader = loaders[suffix]
    except KeyError as exc:
        raise ValueError(
            f"Неподдерживаемый формат: {suffix or 'без расширения'}"
        ) from exc

    text = normalize_text(loader(document))
    if not text:
        raise ValueError(
            "В документе не найден текст. Для сканированного PDF требуется OCR."
        )
    return text


def normalize_text(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\u00a0", " ").replace("\u202f", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"(?<=[а-яёa-z])-\s*\n\s*(?=[а-яёa-z])", "", text, flags=re.IGNORECASE
    )
    text = re.sub(r"[ \t]+", " ", text)

    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n+", text):
        paragraph = " ".join(
            line.strip() for line in block.splitlines() if line.strip()
        )
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def split_into_chunks(
    text: str,
    *,
    max_chars: int = 220,
    sentence_pause_ms: int = 220,
    paragraph_pause_ms: int = 620,
) -> list[TextChunk]:
    if max_chars < 80:
        raise ValueError("Размер фрагмента должен быть не меньше 80 символов")

    normalized = normalize_text(text)
    chunks: list[TextChunk] = []
    paragraphs = [part.strip() for part in normalized.split("\n\n") if part.strip()]

    for paragraph in paragraphs:
        sentences = [
            item.text.strip() for item in sentenize(paragraph) if item.text.strip()
        ]
        if not sentences:
            sentences = [paragraph]

        parts: list[str] = []
        current = ""
        for sentence in sentences:
            for segment in _split_long_segment(sentence, max_chars):
                candidate = f"{current} {segment}".strip()
                if current and len(candidate) > max_chars:
                    parts.append(current)
                    current = segment
                else:
                    current = candidate
        if current:
            parts.append(current)

        for index, part in enumerate(parts):
            pause = paragraph_pause_ms if index == len(parts) - 1 else sentence_pause_ms
            chunks.append(TextChunk(part, pause))

    return chunks


def _split_long_segment(text: str, max_chars: int) -> list[str]:
    remaining = text.strip()
    result: list[str] = []
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        cut = max(window.rfind(mark) for mark in ("; ", ": ", ", ", " — ", " - "))
        if cut < max_chars // 2:
            cut = window.rfind(" ")
        if cut < max_chars // 3:
            cut = max_chars
        part = remaining[:cut].strip(" ,;:-")
        if part:
            result.append(part)
        remaining = remaining[cut:].strip()
    if remaining:
        result.append(remaining)
    return result


def _load_text(path: Path) -> str:
    from charset_normalizer import from_bytes

    raw = path.read_bytes()
    match = from_bytes(raw).best()
    if match is None:
        return raw.decode("utf-8", errors="replace")
    return str(match)


def _load_fb2(path: Path) -> str:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    root = etree.parse(str(path), parser)
    nodes = root.xpath(
        "//*[local-name()='body']//*[local-name()='p' or local-name()='subtitle']"
    )
    return "\n\n".join(
        " ".join(node.itertext()).strip()
        for node in nodes
        if "".join(node.itertext()).strip()
    )


def _load_epub(path: Path) -> str:
    from bs4 import BeautifulSoup
    from ebooklib import ITEM_DOCUMENT, epub

    book = epub.read_epub(str(path))
    paragraphs: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for unwanted in soup(["script", "style", "nav"]):
            unwanted.decompose()
        for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            value = node.get_text(" ", strip=True)
            if value:
                paragraphs.append(value)
    return "\n\n".join(paragraphs)


def _load_docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    return "\n\n".join(
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    )


def _load_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join(
        text for page in reader.pages if (text := (page.extract_text() or "").strip())
    )
