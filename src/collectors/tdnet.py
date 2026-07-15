from __future__ import annotations

import json
import os
import re
from hashlib import sha1
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Any

from src.collectors.utils import USER_AGENT, normalize_code, request_get
from src.core.bizday import JST, today_jst


YANOSHIN_LIST_URL = os.getenv("YANOSHIN_TDNET_LIST_URL", "https://webapi.yanoshin.jp/tdnet/list/{yyyymmdd}.json")
TDNET_HTML_URL = os.getenv("TDNET_HTML_URL", "https://www.release.tdnet.info/inbs/I_list_001_{yyyymmdd}.html")

PO_INCLUDE_KEYWORDS = ["公募", "募集による新株式発行", "売出し", "売出", "株式の売出し"]
PO_EXCLUDE_KEYWORDS = ["立会外分売", "株主割当", "行使価額修正条項", "第三者割当"]
BUYBACK_KEYWORDS = ["自己株式の取得", "自己株式取得", "自己株式取得に係る事項"]


@dataclass
class Disclosure:
    id: str
    code: str
    name: str
    title: str
    announced_at: datetime
    pdf_url: str | None = None
    source_url: str | None = None


def classify_title(title: str) -> set[str]:
    normalized = re.sub(r"\s+", "", title or "")
    classes: set[str] = set()
    if is_po_title(normalized):
        classes.add("po")
    if is_po_pricing_title(normalized):
        classes.add("po_pricing")
    if "立会外分売" in normalized:
        classes.add("bunbai")
    if "転換社債型新株予約権付社債" in normalized or "CB発行" in normalized:
        classes.add("cb")
    if "株式分割" in normalized:
        classes.add("split")
    if contains_buyback(normalized):
        classes.add("buyback")
    return classes


def is_po_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title or "")
    if is_po_pricing_title(normalized):
        return False
    if not any(keyword in normalized for keyword in PO_INCLUDE_KEYWORDS):
        return False
    if any(keyword in normalized for keyword in PO_EXCLUDE_KEYWORDS):
        return False
    if "自己株式の処分" in normalized and not any(keyword in normalized for keyword in ["公募", "売出", "新株式発行"]):
        return False
    return True


def is_po_pricing_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title or "")
    price_keywords = ["発行価格", "売出価格", "発行価格等", "発行価額", "売出価額"]
    return any(keyword in normalized for keyword in price_keywords) and "決定" in normalized


def contains_buyback(text: str | None) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    return any(keyword in normalized for keyword in BUYBACK_KEYWORDS)


def fetch_disclosures(target_date: date | None = None) -> list[Disclosure]:
    day = target_date or today_jst()
    yyyymmdd = day.strftime("%Y%m%d")
    try:
        disclosures = fetch_yanoshin_disclosures(yyyymmdd)
        if disclosures:
            return disclosures
    except Exception:
        pass
    return fetch_tdnet_html_disclosures(yyyymmdd)


def fetch_yanoshin_disclosures(yyyymmdd: str) -> list[Disclosure]:
    url = YANOSHIN_LIST_URL.format(yyyymmdd=yyyymmdd)
    payload = json.loads(request_get(url).decode("utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        known_keys = ("items", "disclosures", "tdnet", "data")
        if not any(key in payload for key in known_keys):
            raise ValueError("Unknown yanoshin response schema")
        rows = next((payload.get(key) for key in known_keys if payload.get(key) is not None), [])
    else:
        raise ValueError("Unknown yanoshin response type")
    if not isinstance(rows, list):
        raise ValueError("Yanoshin disclosure rows are not a list")

    disclosures: list[Disclosure] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_json_disclosure(row)
        if normalized:
            disclosures.append(normalized)
    return disclosures


def _normalize_json_disclosure(row: dict[str, Any]) -> Disclosure | None:
    disclosure_id = str(row.get("id") or row.get("disclosure_id") or row.get("tdnet_id") or row.get("XbrlFilingNumber") or "")
    title = str(row.get("title") or row.get("Title") or "")
    code = normalize_code(row.get("company_code") or row.get("code") or row.get("Code") or row.get("ticker"))
    if not disclosure_id or not title or not code:
        return None
    raw_date = row.get("datetime") or row.get("published_at") or row.get("pubdate") or row.get("date") or row.get("Date")
    raw_time = row.get("time") or row.get("Time")
    raw_datetime = f"{raw_date} {raw_time}" if raw_date and raw_time else raw_date
    try:
        announced_at = _parse_datetime(raw_datetime)
    except ValueError:
        return None
    return Disclosure(
        id=disclosure_id,
        code=code,
        name=str(row.get("company_name") or row.get("name") or row.get("CompanyName") or ""),
        title=title,
        announced_at=announced_at,
        pdf_url=row.get("pdf_url") or row.get("pdf") or row.get("url") or row.get("PdfUrl"),
        source_url=row.get("source_url") or row.get("detail_url"),
    )


def fetch_tdnet_html_disclosures(yyyymmdd: str) -> list[Disclosure]:
    from bs4 import BeautifulSoup
    from src.collectors.utils import absolute_url

    url = TDNET_HTML_URL.format(yyyymmdd=yyyymmdd)
    html = request_get(url).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    disclosures: list[Disclosure] = []
    rows = soup.select("tr")
    if not rows:
        raise RuntimeError("TDnet fallback page contains no table rows")
    header_found = False
    for row in rows:
        # The TDnet page contains an outer layout <tr> around the disclosure
        # table. Only direct cells belong to the current row.
        cell_nodes = row.find_all(["td", "th"], recursive=False)
        cells = [cell.get_text(" ", strip=True) for cell in cell_nodes]
        if len(cells) >= 4 and cells[:4] == ["時刻", "コード", "会社名", "表題"]:
            header_found = True
            continue
        if len(cells) < 4 or not re.fullmatch(r"\d{1,2}:\d{2}", cells[0]):
            continue
        code = normalize_code(cells[1])
        title_node = cell_nodes[3]
        title = title_node.get_text(" ", strip=True)
        link = title_node.find("a", href=re.compile(r"\.pdf(?:$|[?#])", re.I))
        if not code or not title or not link:
            continue
        pdf_url = absolute_url(url, link["href"])
        disclosure_id = _html_disclosure_id(link["href"], yyyymmdd, code, title)
        disclosures.append(
            Disclosure(
                id=disclosure_id,
                code=code,
                name=cells[2],
                title=title,
                announced_at=_parse_datetime(f"{yyyymmdd}{cells[0].replace(':', '')}"),
                pdf_url=pdf_url,
                source_url=url,
            )
        )
    if not header_found:
        raise RuntimeError("TDnet fallback page schema is unrecognized")
    return disclosures


def fetch_pdf_text(pdf_url: str | None) -> str:
    if not pdf_url:
        return ""
    import pdfplumber

    content = request_get(pdf_url)
    parts: list[str] = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(JST) if value.tzinfo else value.replace(tzinfo=JST)
    text = str(value or "")
    for fmt, size in (
        ("%Y%m%d%H%M", 12),
        ("%Y%m%d", 8),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            parsed = datetime.strptime(text[:size], fmt)
            return parsed.replace(tzinfo=JST)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(JST) if parsed.tzinfo else parsed.replace(tzinfo=JST)
    except ValueError as exc:
        raise ValueError(f"Invalid disclosure datetime: {text!r}") from exc


def _html_disclosure_id(seed: str, yyyymmdd: str, code: str, title: str) -> str:
    match = re.search(r"(\d{8,})", seed or "")
    if match:
        return match.group(1)
    digest = sha1(f"{yyyymmdd}:{code}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"tdnet-{yyyymmdd}-{code}-{digest}"


def _guess_name(cells: list[str], code: str) -> str:
    for index, cell in enumerate(cells):
        if code in cell and index + 1 < len(cells):
            return cells[index + 1]
    return ""
