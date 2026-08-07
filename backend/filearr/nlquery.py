"""Natural-language → filter-DSL translation (roadmap §5 P2, 2026-08-06).

Turns a plain-English description ("videos larger than 2GB modified last
week, not tagged archived") into the shared query DSL (``querydsl.py``) that
the Filter Builder / custom reports / LLM facade already execute. Two engines:

* **Heuristic (always available).** A deterministic, ordered pattern pass —
  no model, no network, no non-determinism. It covers the vocabulary people
  actually type (kinds, extensions, sizes, dates, tags, resolution, negation,
  quoted phrases) and leaves anything it cannot claim as free-text terms, so
  the result is always a *valid* DSL string (re-parsed before returning —
  the translator can never hand the UI a string the grammar rejects).
* **Optional local LLM.** When ``FILEARR_NL_OLLAMA_URL`` is set, the text is
  first offered to a local Ollama model with a grammar-teaching system prompt;
  the reply is validated with ``querydsl.parse`` and **falls back to the
  heuristic on any failure** (bad JSON, invalid DSL, timeout, refusal). The
  LLM is an accuracy upgrade, never a dependency — same stance as semantic
  search (local only; no cloud egress for file names).

Pure translation module: no DB, no FastAPI. The endpoint lives in
``api/query.py``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from filearr.file_groups import EXT_GROUP_MAP, FILE_CATEGORIES, FILE_GROUPS
from filearr.querydsl import ParseError, parse

log = logging.getLogger("filearr.nlquery")

# --------------------------------------------------------------------------- #
# Vocabulary                                                                  #
# --------------------------------------------------------------------------- #

#: word -> category key (kind: filter). Singular/plural folded by the caller.
KIND_SYNONYMS: dict[str, str] = {
    "video": "video", "movie": "video", "film": "video", "clip": "video",
    "recording": "video",
    "image": "image", "photo": "image", "picture": "image", "pic": "image",
    "screenshot": "image", "wallpaper": "image",
    "audio": "audio", "music": "audio", "song": "audio", "track": "audio",
    "podcast": "audio",
    "document": "document", "doc": "document", "paper": "document",
    "archive": "archive",
    "executable": "system", "binary": "system",
}

#: word -> group key (group: filter) — used when the word names a group more
#: specific than its category. Checked BEFORE the kind map.
GROUP_SYNONYMS: dict[str, str] = {
    "spreadsheet": "spreadsheet",
    "presentation": "presentation", "slide": "presentation",
    "deck": "presentation",
    "ebook": "ebook", "book": "ebook",
    "comic": "comic",
    "subtitle": "subtitle", "caption": "subtitle",
    "audiobook": "audiobook",
    "playlist": "playlist",
    "font": "font",
    "script": "script",
    "database": "database",
    "email": "email", "mail": "email",
    "iso": "disk-image",
}

#: Vague size adjectives (only applied when no explicit size was matched).
SIZE_ADJECTIVES: dict[str, str] = {
    "huge": "size:>1G",
    "large": "size:>100M",
    "big": "size:>100M",
    "small": "size:<10M",
    "tiny": "size:<1M",
}

#: Resolution shorthand -> meta.height floor.
RESOLUTION_WORDS: dict[str, str] = {
    "4k": "meta.height:>=2000",
    "uhd": "meta.height:>=2000",
    "1080p": "meta.height:>=1080",
    "720p": "meta.height:>=720",
    "hd": "meta.height:>=720",
}

_STOPWORDS = frozenset(
    """a an the all any my me our i we you show find search list get give
    file files item items stuff thing things that which are is was were being
    in on of for from with and to please can could would like want need looking
    look than then it its this those these there here everything anything only
    just some kind type sort or ago recently
    modified edited changed updated created added taken downloaded imported""".split()
)

#: Words that flip the following recognised token to negated.
_NEGATORS = frozenset({"not", "no", "without", "except", "excluding", "minus"})

#: Words that pick created: over the default modified: for a nearby time phrase.
_CREATED_WORDS = ("created", "added", "taken", "downloaded", "imported")

_SIZE_UNIT = {
    "b": 1, "byte": 1, "bytes": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024**2, "mb": 1024**2, "mib": 1024**2, "meg": 1024**2,
    "megs": 1024**2,
    "g": 1024**3, "gb": 1024**3, "gib": 1024**3, "gig": 1024**3,
    "gigs": 1024**3,
    "t": 1024**4, "tb": 1024**4, "tib": 1024**4,
}

_TIME_UNIT_DAYS = {
    "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365,
}


@dataclass
class Translation:
    """The result of a translation attempt. ``dsl`` is ALWAYS parseable."""

    dsl: str
    source: str  # "heuristic" | "ollama"
    filters: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Size / time helpers                                                         #
# --------------------------------------------------------------------------- #
def _size_literal(value: float, unit: str) -> str:
    """Render bytes as the DSL's integer-mantissa binary literal.

    The grammar takes integer mantissas only, so ``1.5 GB`` becomes ``1536M``.
    """
    total = int(round(value * _SIZE_UNIT[unit]))
    for suffix, mult in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if total >= mult and total % mult == 0:
            return f"{total // mult}{suffix}"
    # Not an even multiple: fall to the largest unit that keeps an integer
    # mantissa without loss beyond 1 unit (K precision is plenty for search).
    if total >= 1024:
        return f"{max(1, total // 1024)}K"
    return str(total)


_SIZE_CMP_RE = re.compile(
    r"""\b(?P<dir>larger|bigger|greater|more|over|above|exceeding|at\s+least|
        smaller|less|under|below|at\s+most|tighter)\s*(?:than)?\s+
        (?P<num>\d+(?:\.\d+)?)\s*(?P<unit>tib|tb|t|gigs?|gib|gb|g|megs?|mib|mb|m|kib|kb|k|bytes?|b)\b""",
    re.IGNORECASE | re.VERBOSE,
)
_SIZE_BETWEEN_RE = re.compile(
    r"""\bbetween\s+(?P<lo>\d+(?:\.\d+)?)\s*(?P<lu>tib|tb|t|gib|gb|g|mib|mb|m|kib|kb|k)?\s*
        and\s+(?P<hi>\d+(?:\.\d+)?)\s*(?P<hu>tib|tb|t|gib|gb|g|mib|mb|m|kib|kb|k)\b""",
    re.IGNORECASE | re.VERBOSE,
)

_GTE_WORDS = ("at least",)
_LTE_WORDS = ("at most",)
_GT_WORDS = ("larger", "bigger", "greater", "more", "over", "above", "exceeding")


def _size_pass(text: str, filters: list[str]) -> str:
    def between(m: re.Match) -> str:
        lo_u = (m.group("lu") or m.group("hu")).lower()
        hi_u = m.group("hu").lower()
        filters.append(
            "size:"
            + _size_literal(float(m.group("lo")), lo_u)
            + ".."
            + _size_literal(float(m.group("hi")), hi_u)
        )
        return " "

    text = _SIZE_BETWEEN_RE.sub(between, text)

    def cmp(m: re.Match) -> str:
        d = re.sub(r"\s+", " ", m.group("dir").lower())
        if d in _GTE_WORDS:
            op = ">="
        elif d in _LTE_WORDS:
            op = "<="
        elif d in _GT_WORDS:
            op = ">"
        else:
            op = "<"
        filters.append(f"size:{op}{_size_literal(float(m.group('num')), m.group('unit').lower())}")
        return " "

    return _SIZE_CMP_RE.sub(cmp, text)


_LAST_RE = re.compile(
    r"\b(?:in\s+the\s+|the\s+)?(?:last|past)\s+(?P<n>\d+)?\s*(?P<unit>hour|day|week|month|year)s?\b",
    re.IGNORECASE,
)
_OLDER_RE = re.compile(
    r"\bolder\s+than\s+(?P<n>\d+)\s*(?P<unit>hour|day|week|month|year)s?\b",
    re.IGNORECASE,
)
_SINCE_RE = re.compile(r"\bsince\s+(?P<date>\d{4}(?:-\d{2}-\d{2})?)\b", re.IGNORECASE)
_BEFORE_YEAR_RE = re.compile(r"\bbefore\s+(?P<year>\d{4})\b", re.IGNORECASE)
_IN_YEAR_RE = re.compile(r"\b(?:in|from|during)\s+(?P<year>(?:19|20)\d{2})\b", re.IGNORECASE)


def _time_key(text: str) -> str:
    low = text.lower()
    return "created" if any(w in low for w in _CREATED_WORDS) else "modified"


def _time_pass(text: str, filters: list[str]) -> str:
    key = _time_key(text)

    def last(m: re.Match) -> str:
        n = int(m.group("n") or 1)
        unit = m.group("unit").lower()
        if unit == "hour":
            filters.append(f"{key}:<{n}h")
        else:
            filters.append(f"{key}:<{n * _TIME_UNIT_DAYS[unit]}d")
        return " "

    def older(m: re.Match) -> str:
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        if unit == "hour":
            filters.append(f"{key}:>{n}h")
        else:
            filters.append(f"{key}:>{n * _TIME_UNIT_DAYS[unit]}d")
        return " "

    def since(m: re.Match) -> str:
        d = m.group("date")
        filters.append(f"{key}:>={d if '-' in d else d + '-01-01'}")
        return " "

    def before(m: re.Match) -> str:
        filters.append(f"{key}:<{m.group('year')}-01-01")
        return " "

    def in_year(m: re.Match) -> str:
        y = m.group("year")
        filters.append(f"{key}:{y}-01-01..{y}-12-31")
        return " "

    text = _LAST_RE.sub(last, text)
    text = _OLDER_RE.sub(older, text)
    text = _SINCE_RE.sub(since, text)
    text = _BEFORE_YEAR_RE.sub(before, text)
    text = _IN_YEAR_RE.sub(in_year, text)
    phrase_windows = {
        r"\btoday\b": "<1d",
        r"\byesterday\b": "1d..2d",
        r"\bthis\s+week\b": "<7d",
        r"\bthis\s+month\b": "<30d",
        r"\bthis\s+year\b": "<365d",
    }
    for pattern, window in phrase_windows.items():

        def consume(_m: re.Match, window: str = window) -> str:
            filters.append(f"{key}:{window}")
            return " "

        text = re.sub(pattern, consume, text, flags=re.IGNORECASE)
    return text


_TAG_RE = re.compile(
    r"\b(?P<neg>not\s+|un)?tagged\s+(?:as\s+|with\s+)?(?P<tag>[\w-]+)\b"
    r"|\bwith\s+(?:the\s+)?tag\s+(?P<tag2>[\w-]+)\b",
    re.IGNORECASE,
)

_QUOTE_RE = re.compile(r'"([^"]*)"')


# --------------------------------------------------------------------------- #
# Heuristic translator                                                        #
# --------------------------------------------------------------------------- #
def translate_heuristic(text: str) -> Translation:
    """Deterministic NL → DSL. The returned ``dsl`` always parses."""
    filters: list[str] = []
    terms: list[str] = []
    notes: list[str] = []

    # 1. Quoted phrases become verbatim free-text terms up front.
    def keep_quote(m: re.Match) -> str:
        phrase = m.group(1).strip()
        if phrase:
            terms.append(f'"{phrase}"')
        return " "

    working = _QUOTE_RE.sub(keep_quote, text)

    # 2. Multi-word patterns (each consumes its span from the working text).
    def tag(m: re.Match) -> str:
        name = (m.group("tag") or m.group("tag2") or "").lower()
        if name:
            filters.append(("-" if m.group("neg") else "") + f"tag:{name}")
        return " "

    working = _TAG_RE.sub(tag, working)
    working = _size_pass(working, filters)
    working = _time_pass(working, filters)

    # 3. Word-level pass over what's left.
    words = re.findall(r"[a-z0-9_.'-]+", working.lower())
    kinds_seen: list[str] = []
    exts: list[str] = []
    neg_exts: list[str] = []
    negate_next = False
    explicit_size = any(f.lstrip("-").startswith("size:") for f in filters)

    for raw in words:
        w = raw.strip(".")
        if w.endswith("'s"):
            w = w[:-2]
        if not w:
            continue
        if w in _NEGATORS:
            negate_next = True
            continue
        # Stopwords vanish BEFORE the extension lookup (several are also obscure
        # registered extensions — 'for' is Fortran source) and WITHOUT consuming
        # a pending negation ("without any zips" still negates zips).
        if w in _STOPWORDS:
            continue
        neg = "-" if negate_next else ""
        negate_next = False

        singular = w[:-1] if w.endswith("s") and len(w) > 3 else w
        if w in RESOLUTION_WORDS:
            filters.append(neg + RESOLUTION_WORDS[w])
            continue
        if singular in GROUP_SYNONYMS and GROUP_SYNONYMS[singular] in FILE_GROUPS:
            filters.append(neg + f"group:{GROUP_SYNONYMS[singular]}")
            continue
        if singular in KIND_SYNONYMS and KIND_SYNONYMS[singular] in FILE_CATEGORIES:
            kind = KIND_SYNONYMS[singular]
            if neg:
                filters.append(f"-kind:{kind}")
            elif kind not in kinds_seen:
                kinds_seen.append(kind)
            continue
        if w in EXT_GROUP_MAP:
            (neg_exts if neg else exts).append(w)
            continue
        if not explicit_size and w in SIZE_ADJECTIVES:
            filters.append(neg + SIZE_ADJECTIVES[w])
            continue
        terms.append((neg + raw) if neg else raw)

    # One kind: filter only — the DSL AND-combines, so a second kind would
    # return nothing. Extra kind words are surfaced as a note instead.
    if kinds_seen:
        filters.append(f"kind:{kinds_seen[0]}")
        if len(kinds_seen) > 1:
            notes.append(
                "multiple kinds mentioned ("
                + ", ".join(kinds_seen)
                + ") — filters AND together, so only the first was used"
            )
    if exts:
        filters.append("ext:" + ";".join(dict.fromkeys(exts)))
    if neg_exts:
        filters.append("-ext:" + ";".join(dict.fromkeys(neg_exts)))

    dsl = " ".join(filters + terms).strip()

    # 4. The translator's contract: never return an unparseable string. Any
    #    parse failure here is a translator bug — degrade to quoted free text
    #    (always valid) instead of surfacing a grammar error to the UI.
    if dsl:
        try:
            parse(dsl)
        except ParseError:  # pragma: no cover - defensive; patterns emit valid DSL
            log.warning("nlquery heuristic produced invalid DSL %r; degrading", dsl)
            safe = text.replace('"', " ").strip()
            dsl = f'"{safe}"' if safe else ""
            filters, terms = [], [dsl] if dsl else []

    return Translation(dsl=dsl, source="heuristic", filters=filters, terms=terms, notes=notes)


# --------------------------------------------------------------------------- #
# Optional local-LLM engine (Ollama)                                          #
# --------------------------------------------------------------------------- #
_OLLAMA_SYSTEM = """You translate natural-language file-search requests into a
strict filter DSL. Output ONLY a JSON object {"dsl": "..."} — no prose.

DSL tokens (whitespace-separated, AND-combined; prefix - negates a token):
  kind:<video|image|audio|document|archive|development|system|three-d-cad>
  group:<file-group>   ext:<a;b;c>   tag:<name>   path:<glob>
  size:<cmp><N[K|M|G|T]>   size:<A>..<B>          (binary units, integer)
  modified:<cmp><N[h|d|w]> | modified:<cmp>YYYY-MM-DD | A..B ranges
  created: (same shapes)      meta.<key>:<cmp><value>   cf.<name>:<value>
  cmp is one of > >= < <= (default =). modified:<7d means "within 7 days";
  modified:>30d means "older than 30 days".
Free words = search terms; use "quoted phrases" for exact phrases.
Only emit filters the request clearly implies; put everything else in terms.
Examples:
  "videos over 2 gigs from last week" -> {"dsl": "kind:video size:>2G modified:<7d"}
  "pdf or docx invoices, not tagged done" -> {"dsl": "ext:pdf;docx -tag:done invoice"}
"""


#: HTTP budget for the optional local model — long enough for a cold small
#: model on CPU, short enough that the UI's fallback stays responsive.
_OLLAMA_TIMEOUT_S = 20.0


async def translate_ollama(text: str, *, url: str, model: str) -> Translation | None:
    """Ask a local Ollama model; return None on ANY failure (caller falls back)."""
    import httpx

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _OLLAMA_SYSTEM},
            {"role": "user", "content": text},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT_S) as client:
            resp = await client.post(url.rstrip("/") + "/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        dsl = str(json.loads(content).get("dsl", "")).strip()
        if not dsl:
            return None
        parse(dsl)  # invalid model output -> fall back
        return Translation(dsl=dsl, source="ollama")
    except Exception as exc:  # noqa: BLE001 - degrade to heuristic on any failure
        log.info("nlquery ollama translation unavailable (%s); using heuristic", exc)
        return None


async def translate(text: str, *, ollama_url: str = "", ollama_model: str = "") -> Translation:
    """Translate with the best available engine (LLM first when configured)."""
    text = text.strip()
    if not text:
        return Translation(dsl="", source="heuristic")
    if ollama_url and ollama_model:
        result = await translate_ollama(text, url=ollama_url, model=ollama_model)
        if result is not None:
            return result
    return translate_heuristic(text)
