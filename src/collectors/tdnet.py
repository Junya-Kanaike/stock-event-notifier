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


YANOSHIN_LIST_URL = os.getenv(
    "YANOSHIN_TDNET_LIST_URL", "https://webapi.yanoshin.jp/webapi/tdnet/list/{yyyymmdd}.json"
)
TDNET_HTML_URL = os.getenv("TDNET_HTML_URL", "https://www.release.tdnet.info/inbs/I_list_001_{yyyymmdd}.html")
MAX_TDNET_PAGES = int(os.getenv("TDNET_MAX_PAGES", "10"))
YANOSHIN_RESULT_CAP = int(os.getenv("YANOSHIN_RESULT_CAP", "300"))

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
    if is_po_correction_title(normalized):
        classes.add("po_correction")
    else:
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


def is_po_correction_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title or "")
    if "訂正" not in normalized:
        return False
    if any(keyword in normalized for keyword in PO_EXCLUDE_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in PO_INCLUDE_KEYWORDS)


def is_po_pricing_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title or "")
    if "決定" not in normalized or "仮条件" in normalized:
        return False

    price_keywords = ["発行価格", "売出価格", "発行価額", "売出価額"]
    if any(keyword in normalized for keyword in price_keywords):
        return True

    # REITなどでは「発行価格」と明記せず「価格等の決定」とだけ表現
    # されるため、募集・売出しの文脈がある場合に限って価格決定として扱う。
    offering_context = ["新株式発行", "新投資口発行", "株式売出し", "株式の売出し", "投資口売出し", "公募"]
    return "価格等の決定" in normalized and any(keyword in normalized for keyword in offering_context)


def contains_buyback(text: str | None) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    return any(keyword in normalized for keyword in BUYBACK_KEYWORDS)


def fetch_disclosures(target_date: date | None = None) -> list[Disclosure]:
    day = target_date or today_jst()
    yyyymmdd = day.strftime("%Y%m%d")
    try:
        disclosures = fetch_yanoshin_disclosures(yyyymmdd)
        if 0 < len(disclosures) < YANOSHIN_RESULT_CAP:
            return disclosures
    except Exception:
        return fetch_tdnet_html_disclosures(yyyymmdd)
    html_disclosures = fetch_tdnet_html_disclosures(yyyymmdd)
    if not disclosures:
        return html_disclosures
    by_id = {item.id: item for item in [*disclosures, *html_disclosures]}
    return list(by_id.values())


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
        payload = row.get("Tdnet", row)
        if not isinstance(payload, dict):
            continue
        normalized = _normalize_json_disclosure(payload)
        if normalized:
            disclosures.append(normalized)
    return disclosures


def _normalize_json_disclosure(row: dict[str, Any]) -> Disclosure | None:
    raw_pdf_url = row.get("pdf_url") or row.get("pdf") or row.get("document_url") or row.get("url") or row.get("PdfUrl")
    pdf_url = _unwrap_yanoshin_url(raw_pdf_url)
    pdf_id_match = re.search(r"(\d{8,})\.pdf", str(pdf_url or ""), re.I)
    disclosure_id = (
        pdf_id_match.group(1)
        if pdf_id_match
        else str(row.get("id") or row.get("disclosure_id") or row.get("tdnet_id") or row.get("XbrlFilingNumber") or "")
    )
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
        pdf_url=pdf_url,
        source_url=row.get("source_url") or row.get("detail_url"),
    )


def _unwrap_yanoshin_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value)
    marker = "/rd.php?"
    if marker in url:
        from urllib.parse import unquote

        return unquote(url.split(marker, 1)[1])
    return url


def fetch_tdnet_html_disclosures(yyyymmdd: str) -> list[Disclosure]:
    from bs4 import BeautifulSoup

    url = TDNET_HTML_URL.format(yyyymmdd=yyyymmdd)
    html = request_get(url).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    page_urls = _tdnet_page_urls(soup, url, yyyymmdd)
    disclosures = _parse_tdnet_html_page(soup, url, yyyymmdd)
    for page_url in page_urls[: max(0, MAX_TDNET_PAGES - 1)]:
        page_html = request_get(page_url).decode("utf-8", errors="ignore")
        page_soup = BeautifulSoup(page_html, "html.parser")
        disclosures.extend(_parse_tdnet_html_page(page_soup, page_url, yyyymmdd))

    by_id = {item.id: item for item in disclosures}
    return list(by_id.values())


def _parse_tdnet_html_page(soup: Any, url: str, yyyymmdd: str) -> list[Disclosure]:
    from src.collectors.utils import absolute_url

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


def _tdnet_page_urls(soup: Any, base_url: str, yyyymmdd: str) -> list[str]:
    from src.collectors.utils import absolute_url

    filenames: set[str] = set()
    pattern = re.compile(rf"I_list_(\d{{3}})_{re.escape(yyyymmdd)}\.html")
    for node in soup.find_all(attrs={"onclick": True}):
        match = pattern.search(str(node.get("onclick", "")))
        if match and match.group(1) != "001":
            filenames.add(match.group(0))
    return [absolute_url(base_url, name) for name in sorted(filenames)]


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
