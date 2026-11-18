"""Сборка договора и заявки из шаблонов и выпуск PDF.

Шаблон — текстовый файл: '# ' заголовок, '## ' подзаголовок, абзацы через пустую строку,
поля в фигурных скобках. HTML-комментарии <!-- --> выкидываются.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
FONTS = ROOT / "fonts"

_fonts_ready = False


def _ensure_fonts() -> None:
    global _fonts_ready
    if not _fonts_ready:
        pdfmetrics.registerFont(TTFont("DejaVu", str(FONTS / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))
        _fonts_ready = True


class _Blank(dict):
    def __missing__(self, key):
        return "________"


def render_text(template_name: str, fields: dict) -> str:
    raw = (TEMPLATES / template_name).read_text(encoding="utf-8")
    raw = re.sub(r"<!--.*?-->\s*", "", raw, flags=re.S)
    clean = {k: (v if v not in (None, "") else "________") for k, v in fields.items()}
    return raw.format_map(_Blank(clean)).strip() + "\n"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SignatureStamp:
    signer: str
    phone: str
    signed_at: str        # человекочитаемо, по Москве
    doc_sha256: str
    signature_id: str
    executor: str


def write_pdf(text: str, path: Path, stamp: SignatureStamp | None = None) -> Path:
    _ensure_fonts()
    path.parent.mkdir(parents=True, exist_ok=True)
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=9.5, leading=13, spaceAfter=5)
    h1 = ParagraphStyle("h1", parent=base, fontName="DejaVu-Bold", fontSize=13, leading=17,
                        alignment=1, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=base, fontName="DejaVu-Bold", fontSize=10.5,
                        spaceBefore=6)
    box = ParagraphStyle("box", parent=base, fontSize=8.5, leading=11, borderWidth=0.7,
                         borderColor="#333333", borderPadding=6, spaceBefore=14)
    story = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("## "):
            story.append(Paragraph(escape(block[3:]), h2))
        elif block.startswith("# "):
            story.append(Paragraph(escape(block[2:]), h1))
        else:
            story.append(Paragraph(escape(block).replace("\n", "<br/>"), base))
    if stamp:
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph(
            "<b>Документ подписан простой электронной подписью</b><br/>"
            f"Заказчик: {escape(stamp.signer)}, телефон {escape(stamp.phone)}<br/>"
            f"Дата и время подписания: {escape(stamp.signed_at)}<br/>"
            f"Способ: одноразовый код из SMS, введённый в Telegram-боте Исполнителя<br/>"
            f"ID подписи: {escape(stamp.signature_id)}<br/>"
            f"Контрольная сумма текста (SHA-256): {stamp.doc_sha256}<br/>"
            f"Исполнитель: {escape(stamp.executor)}", box))
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=2 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                            title=text.splitlines()[0].lstrip("# "))
    doc.build(story)
    return path
