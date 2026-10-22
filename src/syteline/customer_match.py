"""Find the customer for a PO from its name and shipping address, as the PO OCR demo does (customer_resolver.py).

The rules are the demo's, ported unchanged so the decisions are identical (tests/fixtures/customer_match_golden.json is
generated from the demo's own code): candidate rows come from the customer address data through one parameterised
query (`sqlserver.SqlServer.candidates`), each candidate is scored on name and address with `rapidfuzz`, and the best
candidate is accepted only when its score is at least the confidence threshold AND it leads the runner-up by at least
the ambiguity margin. Thresholds: SYTELINE_MATCH_MIN_SCORE (70), SYTELINE_MATCH_MARGIN (3), SYTELINE_MATCH_CAP (1000).

Result (data-model.md "Customer Match"): {status: match|ambiguous|low_confidence|not_found, cust_num?, cust_seq?,
matched_name?, score?, runner_up?, reason?}. Only `match` may be used to book an order.
"""

import re

from rapidfuzz import fuzz

from . import config

_NAME_STOPWORDS = {
    "corp", "corporation", "co", "company", "inc", "incorporated",
    "ltd", "limited", "llc", "lp", "the", "and", "of", "for",
    "international", "intl", "services", "service", "group",
    "aviation", "aerospace", "airlines", "airways",
}

_STREET_ABBR = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave",
    "boulevard": "blvd", "blvd.": "blvd",
    "road": "rd", "drive": "dr", "lane": "ln", "court": "ct",
    "suite": "ste", "ste.": "ste",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "p.o.": "po", "po.": "po",
    "corp": "corporation", "co": "company", "inc": "incorporated",
}

_NOISE_RE = re.compile("|".join([
    r"^\s*ship\s*via\s*[:|]",
    r"^\s*carrier\s*(account)?\s*[:|]",
    r"^\s*freight\s*[:|]",
    r"^\s*account\s*[:|]",
    r"^\s*phone\s*[:|]",
    r"^\s*fax\s*[:|]",
    r"^\s*attn\s*[:|]",
    r"^\s*tel\s*[:|]",
    r"^\s*incoterms?\s*[:|]",
    r"^\s*tracking\s*[:|]",
]), re.IGNORECASE)


def name_tokens(name):
    """Lower-case words of the name, 3+ characters, no stop words, no repeats (the search terms; the caller caps at 5)."""
    if not name:
        return []
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    tokens, seen = [], set()
    for t in cleaned.split():
        if len(t) < 3 or t in _NAME_STOPWORDS or t in seen:
            continue
        seen.add(t)
        tokens.append(t)
    return tokens


def zip_token(address):
    if not address:
        return None
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
    return m.group(1) if m else None


def _strip_noise(text):
    return "\n".join(line for line in text.splitlines() if line.strip() and not _NOISE_RE.match(line))


def normalize(text):
    if not text:
        return ""
    s = re.sub(r"[.,#/]", " ", str(text).lower())
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(_STREET_ABBR.get(t, t) for t in s.split())


def clean_address(address):
    cleaned = _strip_noise(address or "")
    return " ".join(ln.strip() for ln in cleaned.splitlines() if ln.strip())


def candidate_name(row):
    name = row.get("name", "") or ""
    addr1 = row.get("addr1", "") or ""
    if addr1 and not re.match(r"^\s*\d", addr1):
        return f"{name} {addr1}"
    return name


def candidate_address(row):
    parts = [row.get("addr1"), row.get("addr2"), row.get("addr3"), row.get("addr4"),
             row.get("city"), row.get("state"), row.get("zip"), row.get("country")]
    return " ".join(p for p in parts if p)


def score(name_n, addr_n, row):
    cand_name_n = normalize(candidate_name(row))
    cand_addr_n = normalize(candidate_address(row))
    name_score = 0.0
    if name_n and cand_name_n:
        name_score = max(fuzz.token_set_ratio(name_n, cand_name_n), fuzz.partial_ratio(name_n, cand_name_n))
    addr_score = 0.0
    if addr_n and cand_addr_n:
        addr_score = 0.6 * fuzz.token_set_ratio(addr_n, cand_addr_n) + 0.4 * fuzz.partial_ratio(addr_n, cand_addr_n)
    if not addr_n:
        return name_score
    if not name_n:
        return addr_score
    return 0.5 * name_score + 0.5 * addr_score


def _ref(row, sc):
    return {"cust_num": str(row.get("cust_num", "")).strip(), "cust_seq": row.get("cust_seq"),
            "name": row.get("name", ""), "score": round(sc, 2)}


def decide(name, address, rows, min_score=70.0, margin=3.0):
    """The Customer Match for `rows` (the candidates already fetched)."""
    if not rows:
        return {"status": "not_found", "reason": "no candidate rows in the customer address data"}
    name_n, addr_n = normalize(name.strip()), normalize(clean_address(address.strip()))
    scored = sorted(((r, score(name_n, addr_n, r)) for r in rows), key=lambda x: x[1], reverse=True)
    (best, best_score) = scored[0]
    (runner, runner_score) = scored[1] if len(scored) > 1 else (None, 0.0)
    if best_score >= min_score and best_score - runner_score >= margin:
        status = "match"
    elif best_score >= min_score:
        status = "ambiguous"
    else:
        status = "low_confidence"
    out = {"status": status, "score": round(best_score, 2), "best": _ref(best, best_score),
           "runner_up": None if runner is None else _ref(runner, runner_score)}
    out.update(cust_num=out["best"]["cust_num"], cust_seq=best.get("cust_seq"), matched_name=best.get("name", ""))
    return out


def address_text(customer):
    """The free-text ship-to address the demo scores: address line, then city, state and postal code."""
    tail = " ".join(p for p in (customer.get("city"), customer.get("state"), customer.get("postal_code")) if p)
    return "\n".join(p for p in (customer.get("address1"), tail) if p)


def find(db, customer):
    """Search the customer address data through `db` (a sqlserver.SqlServer) and decide. Sends nothing when the name or
    the address is empty (the demo refuses those too)."""
    name = (customer.get("name") or "").strip()
    address = address_text(customer).strip()
    if not name or not address:
        return {"status": "not_found", "reason": "the name and the shipping address are both needed"}
    min_score, margin, cap = config.match_settings()
    rows = db.candidates(name_tokens(name), zip_token(address), cap)
    result = decide(name, address, rows, min_score, margin)
    result["candidates_considered"] = len(rows)
    return result
