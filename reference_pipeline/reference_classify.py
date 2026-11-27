"""Classify references into BibTeX entry types using conservative heuristics."""
from __future__ import annotations

import re

# Quy ước của nhóm cho "arXiv preprint": "article" (kiểu Google Scholar) hoặc "misc"
ARXIV_ENTRY_TYPE = "article"

# Journal đặc biệt có chữ "proceedings" nhưng KHÔNG phải conference
_JOURNAL_EXCEPTIONS = re.compile(
    r"proceedings on privacy enhancing technologies|"
    r"proc\.?\s+priv\.?\s+enhancing\s+technol|"
    r"proceedings of the national academy of sciences|"
    r"proc\.?\s+natl\.?\s+acad\.?\s+sci"
)

_CONF_ACRONYMS = re.compile(
    r"\b(cvpr|iccv|eccv|neurips|nips|icml|iclr|acl|emnlp|naacl|coling|aaai|ijcai|"
    r"kdd|sigir|sigmod|vldb|icse|esec/fse|icra|iros|interspeech|icassp|"
    r"wacv|bmvc|colt|uai|aistats|recsys|cikm|wsdm|chi|uist|sosp|osdi|nsdi|"
    r"usenix security|ccs|ndss|iswc|eswc)\b"
)

_CONF_PATTERNS = [
    r"\bproceedings of\b",
    r"\bin the proceedings\b",
    r"\bin proceedings\b",
    r"\bin proc\.?\s",
    r"\bproc\.\s+of\b",
    r"\binternational conference\b",
    r"\bconference on\b",
    r"\bsymposium on\b",
    r"\bworkshop on\b",
    r"\bannual meeting of the\b",
    r"\badvances in neural information processing systems\b",
    r"\bproceedings of machine learning research\b",
    r"\bpmlr\b",
    r"\blecture notes in computer science\b",
    r"\bthe web conference\b",
    r"\bspie\b",                   # SPIE proceedings
    r"\bproceedings of spie\b",
]

_JOURNAL_PATTERNS = [
    r"\bjournal of\b",
    r"\bjournal on\b",
    r"\btransactions on\b",
    r"\btransactions of\b",
    r"\bieee access\b",
    r"\bplos one\b",
    r"\bscientific reports\b",
    r"\bnature communications\b",
    r"\bcommunications of the acm\b",
    r"\bacm computing surveys\b",
    r"\bcomputing surveys\b",
    r"\bphysical review\b",
    r"\bjmlr\b",
    r"\bacs photonics\b",
    r"\bcomputers in human behavior\b",
    r"\b(?:letters|reviews|annals) (?:of|in|on)\b",
    # Tên journal ngắn: chỉ nhận khi theo sau là số volume, tránh nhầm với tiêu đề
    r"\b(?:nature|science|sensors|cell)\b\s*,?\s*\d{1,4}\s*(?:\(\d+\))?\s*[:,]",
]

# volume / issue / pages
_VOLUME_PATTERNS = [
    r"\bvol(?:ume)?\.?\s*\d+",
    r"\b\d{1,4}\s*\(\d{1,3}\)\s*:\s*\d+",   # 12(3):45
]

_BOOK_PATTERNS = [
    r"\bpublished by\b",
    r"\bpublisher\b",
    r"\bisbn\b",
    r"\b\d+(?:st|nd|rd|th)\s+edition\b",
    r"\bspringer\b",
    r"\bwiley\b",
    r"\bcrc press\b",
    r"\bmit press\b",
    r"\b(?:cambridge|oxford) university press\b",
    r"\buniversity press\b",
    r"\bmorgan kaufmann\b",
    r"\baddison[- ]wesley\b",
    r"\bprentice hall\b",
    r"\bmcgraw[- ]hill\b",
    r"\bo'reilly\b",
    r"\bpress\b",
]


def _normalize_text(raw: str) -> str:
    """Bỏ lệnh LaTeX + ngoặc, gộp khoảng trắng, hạ chữ thường."""
    text = re.sub(r"\\newblock\b", " ", raw or "")
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = text.replace("{", " ").replace("}", " ").replace("~", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify_reference(raw_content: str, metadata: dict | None = None):
    """
    Return (entry_type, reason).
    Uncertain references fall back to misc.
    """
    metadata = metadata or {}
    text = _normalize_text(raw_content)
    venue = _normalize_text(str(metadata.get("venue") or ""))
    haystack = f"{text} {venue}".strip()

    # 1. Thesis
    if re.search(r"\b(ph\.?\s*d\.?\s*(?:thesis|dissertation)|doctoral (?:thesis|dissertation))\b", haystack):
        return "phdthesis", "doctoral thesis indicator"
    if re.search(r"\bmaster'?s?\s+(?:thesis|dissertation)\b", haystack):
        return "mastersthesis", "master thesis indicator"

    # 2. Technical report
    if re.search(r"\b(technical report|tech\.? report|research report)\b", haystack):
        return "techreport", "technical report indicator"

    # 3. Manual / datasheet
    if re.search(r"\b(datasheet|data sheet|user manual|technical manual)\b", haystack):
        return "manual", "manual or datasheet indicator"

    # 4. Journal có chữ "proceedings" (PoPETs, PNAS) -> article
    if _JOURNAL_EXCEPTIONS.search(haystack):
        return "article", "journal named 'proceedings'"
    
    if str(metadata.get("journal") or "").strip():
        return "article", "journal metadata available"
    if str(metadata.get("booktitle") or "").strip():
        return "inproceedings", "booktitle metadata available"

    # 5. Conference
    if _any(_CONF_PATTERNS, haystack) or _CONF_ACRONYMS.search(haystack):
        return "inproceedings", "conference or proceedings indicator"

    # 6. Journal
    if str(metadata.get("journal") or "").strip():
        return "article", "journal metadata available"
    if _any(_JOURNAL_PATTERNS, haystack):
        return "article", "journal publication indicator"
    if _any(_VOLUME_PATTERNS, haystack):
        return "article", "volume/issue pattern"

    # 7. arXiv preprint
    if re.search(r"\barxiv\b|\bcorr\b", haystack):
        return ARXIV_ENTRY_TYPE, "arXiv/CoRR preprint"

    # 8. Book
    if metadata.get("publisher") or _any(_BOOK_PATTERNS, haystack):
        return "book", "publisher indicator"

    # 9. Online resource
    has_url = bool(re.search(r"https?://", haystack))
    if has_url and re.search(
        r"\b(accessed|online resource|website|web page|available at|retrieved)\b", haystack
    ):
        return "misc", "online resource; standard BibTeX fallback"

    return "misc", "insufficient information for reliable classification"