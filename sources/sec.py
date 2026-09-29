# -*- coding: utf-8 -*-
"""
SEC EDGAR, форма D — официальные заявки о привлечённых деньгах в США.

ЗАЧЕМ. Новости пишут о раунде, когда компания захотела о нём рассказать.
Форму D компания ОБЯЗАНА подать в течение 15 дней после первой продажи
долей — поэтому здесь видны и те раунды, о которых не писал никто, а сумма
не округлена журналистом. Минусы: только США, отрасль грубая («Other
Technology»), и нет стадии — поэтому форма D подтверждает и считает, но
ниши по ней не строятся.

КАК. Дневной индекс EDGAR (form.YYYYMMDD.idx) — список всех поданных за
день форм; из него берём строки типа D (поправки D/A пропускаем: это тот же
раунд). Потом по каждой заявке — primary_doc.xml с цифрами. Заявок сотни в
день, поэтому разбор идёт очередью, по MAX_DOCS за прогон.

ЧЕСТНО ПРО ДОСТУП. SEC требует User-Agent с контактом и режет больше
10 запросов в секунду. С казахстанского адреса EDGAR не ответил вовсе
(2026-09-28), поэтому сбор идёт только из GitHub Actions. Контакт — в
SEC_USER_AGENT (секрет репозитория); без него — ссылка на репозиторий.
"""
import os
import re
import time

import requests

INDEX = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx"
DOC = "https://www.sec.gov/Archives/edgar/data/%s/%s/primary_doc.xml"
FILING = "https://www.sec.gov/Archives/edgar/data/%s/%s/"
MAX_DOCS = 60
MAX_AGE_YEARS = 6          # компания моложе — это стартап, старше — обычный бизнес
MIN_SOLD = 250000

# Отрасли формы D -> секторы market.py. «Other Technology» — почти весь
# софт и ИИ; сектор внутри неё по форме D не определить.
INDUSTRY = {
    "Biotechnology": "health", "Pharmaceuticals": "health", "Other Health Care": "health",
    "Health Insurance": "health", "Hospitals and Physicians": "health",
    "Oil and Gas": "energy", "Coal Mining": "energy", "Electric Utilities": "energy",
    "Energy Conservation": "energy", "Environmental Services": "energy", "Other Energy": "energy",
    "Manufacturing": "hardware", "Agriculture": "hardware",
    "Commercial Banking": "fintech", "Insurance": "fintech", "Investing": "fintech",
    "Investment Banking": "fintech", "Other Banking and Financial Services": "fintech",
    "REITS and Finance": "proptech", "Construction": "proptech", "Commercial": "proptech",
    "Residential": "proptech", "Other Real Estate": "proptech",
    "Retailing": "commerce", "Restaurants": "consumer", "Lodging and Conventions": "consumer",
    "Tourism and Travel Services": "consumer", "Airlines and Airports": "mobility",
    "Other Travel": "consumer",
}
FUNDS = ("Pooled Investment Fund",)


def _ua():
    return (os.environ.get("SEC_USER_AGENT") or "").strip() or \
        "launch-scout market research (github.com/clam83574-commits/launch-scout)"


def _get(url, timeout=25):
    try:
        r = requests.get(url, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"},
                         timeout=timeout)
    except requests.RequestException as e:
        return None, str(e)[:120]
    if r.status_code != 200:
        return None, "HTTP %d" % r.status_code
    return r.text, None


def daily_filings(day_ts):
    """[(cik, accession, company)] форм D за день (по UTC-дате). (список, ошибка)."""
    g = time.gmtime(day_ts)
    stamp = time.strftime("%Y%m%d", g)
    text, err = _get(INDEX % (g.tm_year, (g.tm_mon - 1) // 3 + 1, stamp))
    if err:
        return [], err
    out = []
    for line in text.splitlines():
        if not line.startswith("D "):
            continue
        m = re.search(r"edgar/data/(\d+)/([\d-]+)\.txt\s*$", line)
        if m:
            name = re.sub(r"\s{2,}.*$", "", line[2:].strip())
            out.append((m.group(1), m.group(2), name))
    return out, None


def _tag(xml, name):
    m = re.search(r"<%s>\s*(.*?)\s*</%s>" % (name, name), xml, re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip() if m else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_doc(xml, now):
    """Словарь заявки или None, если это не стартап (фонд, старая компания, мелочь)."""
    industry = _tag(xml, "industryGroupType") or ""
    if industry in FUNDS or "<investmentFundInfo>" in xml:
        return None
    within5 = (_tag(xml, "withinFiveYears") or "").lower() == "true"
    year = _num(_tag(xml, "value") if "<yearOfInc>" in xml else None)
    yoi = re.search(r"<yearOfInc>(.*?)</yearOfInc>", xml, re.S)
    if yoi:
        year = _num(_tag(yoi.group(1), "value"))
    this_year = time.gmtime(now).tm_year
    if not within5 and (year is None or this_year - year > MAX_AGE_YEARS):
        return None
    sold = _num(_tag(xml, "totalAmountSold"))
    if not sold or sold < MIN_SOLD:
        return None
    first = _tag(re.search(r"<dateOfFirstSale>(.*?)</dateOfFirstSale>", xml, re.S).group(1), "value") \
        if "<dateOfFirstSale>" in xml else None
    return {
        "company": (_tag(xml, "entityName") or "").strip()[:100],
        "industry": industry,
        "sector": INDUSTRY.get(industry),
        "sold": sold,
        "offering": _num(_tag(xml, "totalOfferingAmount")),
        "state": _tag(xml, "stateOrCountry") or "",
        "year_inc": int(year) if year else None,
        "first_sale": first,
    }


def fetch_doc(cik, acc, now):
    """(заявка или None, ошибка)."""
    xml, err = _get(DOC % (cik, acc.replace("-", "")))
    if err:
        return None, err
    return parse_doc(xml, now), None


def filing_url(cik, acc):
    return FILING % (cik, acc.replace("-", ""))
