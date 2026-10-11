"""Read-only category discovery and validated creation TSV snapshots."""

from __future__ import annotations

import csv
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests

from .api_client import APIRequestError, WikimediaAPIClient
from .localization import translate_runtime
from ..constants import REQUEST_HEADERS
from ..utils import debug


def message(key: str, **values) -> str:
    return translate_runtime(f"ui.transfer.{key}", key).format(**values)


class TransferStopped(Exception):
    """Cooperative cancellation, distinct from a missing page or API error."""


class CategoryItemConflict(ValueError):
    def __init__(self, source, target, source_qid, target_qid):
        super().__init__(message("item_conflict", source=source, target=target))
        self.source_qid, self.target_qid = source_qid, target_qid


@dataclass(frozen=True)
class CategoryCreationRow:
    source_title: str
    target_title: str
    content: str


@dataclass(frozen=True)
class CategoryPair:
    """The same source/target relationship carried between all three stages."""

    source_title: str
    target_title: str
    qid: str = ""


@dataclass(frozen=True)
class NumberedTitleProposal:
    index: int
    target_title: str
    status: str


_ROMAN_DIGITS = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                 (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def roman_number(value: int) -> str:
    if not 1 <= value <= 3999:
        raise ValueError(message("number_out_of_range"))
    text = ""
    for number, symbol in _ROMAN_DIGITS:
        count, value = divmod(value, number)
        text += symbol * count
    return text


def parse_title_number(raw: str) -> tuple[int, str]:
    digits = re.fullmatch(r"([0-9]+)(?:st|nd|rd|th)?", raw, re.IGNORECASE)
    if digits:
        return int(digits.group(1)), digits.group(1)
    raw = raw.upper()
    if not re.fullmatch(r"[IVXLCDM]+", raw):
        raise ValueError(message("number_template_invalid"))
    values = dict(I=1, V=5, X=10, L=50, C=100, D=500, M=1000)
    number = sum(-values[char] if index + 1 < len(raw) and values[char] < values[raw[index + 1]] else values[char]
                 for index, char in enumerate(raw))
    if roman_number(number) != raw:
        raise ValueError(message("number_template_invalid"))
    return number, str(number)


def title_number_matches(title: str):
    matches = []
    for match in re.finditer(r"(?<![A-Za-z0-9])[0-9]+(?i:st|nd|rd|th)?(?![A-Za-z0-9])|\b[IVXLCDM]+\b", title):
        try:
            parse_title_number(match.group())
        except ValueError:
            continue
        matches.append(match)
    return matches


def plan_numbered_titles(rows: tuple[CategoryCreationRow, ...], source_pattern: str,
                        target_template: str, indices: set[int] | None = None) -> tuple[NumberedTitleProposal, ...]:
    """Preview numeric/ordinal/Roman title conversions without overwriting input.

    An empty source pattern discovers a single number automatically. An exact
    pattern restricts a mixed list to one series; ambiguous titles are skipped.
    """
    aliases = {"{number}": "{число}", "{roman}": "{римская}"}
    for alias, token in aliases.items():
        source_pattern = source_pattern.replace(alias, token)
        target_template = target_template.replace(alias, token)
    source_pattern = source_pattern.strip().replace("_", " ")
    target_template = target_template.strip()
    tokens = ("{число}", "{римская}")
    source_tokens = sum(source_pattern.count(token) for token in tokens)
    target_tokens = sum(target_template.count(token) for token in tokens)
    source_literal, target_literal = source_pattern, target_template
    for token in tokens:
        source_literal = source_literal.replace(token, "")
        target_literal = target_literal.replace(token, "")
    if ((source_pattern and source_tokens != 1) or not target_tokens
            or any(char in source_literal for char in "{}")
            or any(char in target_literal for char in "[]{}|<>\t\r\n#")):
        raise ValueError(message("number_template_invalid"))
    pattern = None
    if source_pattern:
        token = next(token for token in tokens if token in source_pattern)
        before, after = source_pattern.split(token)
        pattern = re.compile(re.escape(before) + r"([0-9]+(?i:st|nd|rd|th)?|[IVXLCDM]+)" + re.escape(after))
    occupied = {row.target_title.strip().replace("_", " ") for row in rows if row.target_title.strip()}
    generated: dict[str, int] = {}
    proposals = []
    for index, row in enumerate(rows):
        if indices is not None and index not in indices:
            continue
        if row.target_title.strip():
            proposals.append(NumberedTitleProposal(index, row.target_title, "number_kept"))
            continue
        title = row.source_title.strip().replace("_", " ")
        matches = [match] if pattern and (match := pattern.fullmatch(title)) else []
        if pattern is None:
            matches = title_number_matches(title)
        if len(matches) != 1:
            proposals.append(NumberedTitleProposal(index, "", "number_unmatched"))
            continue
        try:
            number, digits = parse_title_number(matches[0].group(1) if pattern else matches[0].group())
            translated = target_template.replace("{число}", digits)
            if "{римская}" in translated:
                translated = translated.replace("{римская}", roman_number(number))
        except ValueError:
            proposals.append(NumberedTitleProposal(index, "", "number_unmatched"))
            continue
        key = translated.replace("_", " ")
        generated[key] = generated.get(key, 0) + 1
        proposals.append(NumberedTitleProposal(index, translated, "number_fill"))
    return tuple(NumberedTitleProposal(proposal.index, proposal.target_title, "number_conflict")
                 if proposal.status == "number_fill" and (
                     proposal.target_title.replace("_", " ") in occupied
                     or generated[proposal.target_title.replace("_", " ")] > 1)
                 else proposal for proposal in proposals)


def merge_commands(conflicts: list[dict]) -> str:
    """Deduplicate connected conflicts, keeping the lowest (oldest) item ID."""
    parents = {}
    def root(qid):
        parents.setdefault(qid, qid)
        oldest, path = qid, []
        while parents[oldest] != oldest:
            path.append(oldest)
            oldest = parents[oldest]
        for item in path:
            parents[item] = oldest
        return oldest
    for conflict in conflicts:
        source, target = conflict.get("source_qid", ""), conflict.get("target_qid", "")
        if not all(re.fullmatch(r"Q[1-9][0-9]*", qid) for qid in (source, target)) or source == target:
            continue
        first, second = sorted((root(source), root(target)), key=lambda qid: int(qid[1:]))
        parents[second] = first
    return "\n".join(f"MERGE\t{qid}\t{root(qid)}" for qid in sorted(parents, key=lambda qid: int(qid[1:]))
                     if qid != root(qid))


def read_creation_tsv(path: str, *, allow_untranslated: bool = False) -> tuple[CategoryCreationRow, ...]:
    rows = []
    sources, targets = set(), set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for line, fields in enumerate(csv.reader(handle, delimiter="\t"), 1):
            if not fields or not any(field.strip() for field in fields):
                continue
            if len(fields) < 2 or not fields[0].strip() or (not allow_untranslated and not fields[1].strip()):
                raise ValueError(message("tsv_titles_required", line=line))
            source, target = (field.strip() for field in fields[:2])
            content = "\n".join(fields[2:])
            # Final namespace-aware duplicate checks also run before any writes.
            if source in sources or (target and target in targets):
                raise ValueError(message("tsv_duplicate", line=line))
            sources.add(source)
            if target:
                targets.add(target)
            rows.append(CategoryCreationRow(source, target, content))
    if not rows:
        raise ValueError(message("tsv_empty"))
    return tuple(rows)


def export_creation_tsv(path: str, candidates: list[dict]) -> None:
    """No header: source title, translation to fill in, category wikitext."""
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        for candidate in candidates:
            writer.writerow([candidate["source_title"], candidate.get("target_title", ""), ""])


class CategoryTransferReader:
    """Batched, paginated Wikimedia reads with cancellable bounded retries.

    Counts only existing mainspace non-redirect target pages linked by Wikidata,
    matching the article mode of the content synchronization worker.
    """

    _MAX_PAGE_BATCH = 50
    _MAX_QUERY_URL_BYTES = 6000

    def __init__(self, source_lang: str, target_lang: str, family: str,
                 stop_event: threading.Event, delay: float = 0.5):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.family = family
        self.stop_event = stop_event
        self.delay = max(0.35, float(delay))
        self._request_delay = self.delay
        self._cooldown_until = 0.0
        self.on_retry = lambda _message: None
        self.api = WikimediaAPIClient()
        self._last_request = 0.0
        self._site_info: dict[str, dict] = {}
        self._item_links: dict[str, str] = {}
        self._article_links: dict[str, str] = {}

    def close(self):
        self.api.session.close()

    def check_stop(self):
        if self.stop_event.is_set():
            raise TransferStopped()

    @staticmethod
    def _retry_after(response) -> float:
        """HTTP Retry-After supports either seconds or an absolute HTTP date."""
        value = response.headers.get("Retry-After", "") if response is not None else ""
        if not isinstance(value, str):
            return 0.0
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, date.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return 0.0

    def _wait(self, seconds):
        if self.stop_event.wait(seconds):
            raise TransferStopped()

    @staticmethod
    def _request_error(error, url):
        if isinstance(error, requests.HTTPError) and error.response is not None:
            status = error.response.status_code
            key = "api_uri_too_long" if status == 414 else "api_http_error"
            return APIRequestError(message(key, status=status, url=url))
        return APIRequestError(str(error))

    def request(self, lang: str, params: dict, *, wikidata: bool = False) -> dict:
        url = ("https://www.wikidata.org/w/api.php" if wikidata
               else self.api._build_api_url(self.family, lang))
        query = {"format": "json", "formatversion": 2, **params}
        for attempt in range(1, 5):
            self.check_stop()
            wait = max(0.0, max(self._last_request + self._request_delay, self._cooldown_until) - time.monotonic())
            self._wait(wait)
            response = None
            try:
                self._last_request = time.monotonic()
                debug(f"[Category transfer API] {url}: action={query.get('action')}, "
                      f"module={query.get('list') or query.get('prop') or query.get('meta') or 'entities'}, "
                      f"titles={len(query.get('titles', '').split('|')) if query.get('titles') else 0}, attempt={attempt}")
                response = self.api.session.get(url, params=query, timeout=20,
                                                headers=REQUEST_HEADERS)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    error = data["error"]
                    if error.get("code") not in {"maxlag", "ratelimited", "readonly"}:
                        raise APIRequestError(str(error))
                    raise requests.RequestException(str(error))
                debug(f"[Category transfer API] HTTP {response.status_code}: completed in "
                      f"{time.monotonic() - self._last_request:.2f}s")
                return data
            except (requests.RequestException, ValueError) as error:
                debug(f"[Category transfer API] {type(error).__name__}: "
                      f"HTTP {response.status_code if response is not None else 'unavailable'}, attempt={attempt}")
                if isinstance(error, requests.HTTPError) and error.response is not None:
                    if error.response.status_code not in {429, 500, 502, 503, 504}:
                        raise self._request_error(error, url) from error
                # Wikimedia requires respecting Retry-After on 429/503. Keep
                # the cooldown even when this row exhausts its retry budget.
                backoff = max(self._retry_after(response), min(60.0, 5.0 * 2 ** (attempt - 1)))
                self._cooldown_until = time.monotonic() + backoff
                if response is not None and response.status_code in {429, 503}:
                    self._request_delay = min(10.0, max(0.5, self._request_delay * 1.5))
                self.on_retry(message("retry_wait", seconds=backoff, attempt=attempt, retries=4))
                if attempt == 4:
                    raise self._request_error(error, url) from error
                self._wait(backoff)
        raise APIRequestError("No response")

    def site_info(self, lang: str) -> dict:
        if lang not in self._site_info:
            self._site_info[lang] = self.request(lang, {
                "action": "query", "meta": "siteinfo",
                "siprop": "general|namespaces|namespacealiases",
            })["query"]
        return self._site_info[lang]

    def category_title(self, raw: str, lang: str) -> str:
        raw = raw.strip().lstrip("\ufeff").replace("_", " ")
        if raw.startswith("[[") and raw.endswith("]]"):
            raw = raw[2:-2].split("|", 1)[0].strip()
        info = self.site_info(lang)
        ns = info["namespaces"]["14"]
        prefix = ns.get("name", ns.get("*", "Category")) or "Category"
        aliases = {"category", prefix.casefold(), ns.get("canonical", "Category").casefold()}
        aliases.update(alias.get("alias", alias.get("*", "")).casefold()
                       for alias in info.get("namespacealiases", []) if alias.get("id") == 14)
        first, separator, rest = raw.partition(":")
        if separator and first.casefold() in aliases:
            raw = rest.strip()
        elif separator:
            other_prefixes = {str(value.get("name", value.get("*", ""))).casefold()
                              for key, value in info["namespaces"].items() if key not in {"0", "14"}}
            other_prefixes.update(str(value.get("canonical", "")).casefold()
                                  for key, value in info["namespaces"].items() if key not in {"0", "14"})
            other_prefixes.update(alias.get("alias", alias.get("*", "")).casefold()
                                  for alias in info.get("namespacealiases", []) if alias.get("id") not in {0, 14})
            if first.casefold() in other_prefixes:
                raise ValueError(message("invalid_title", title=raw))
        if not raw or any(char in raw for char in "[]{}|<>\t\r\n#"):
            raise ValueError(message("invalid_title", title=raw))
        if ns.get("case", "first-letter") == "first-letter":
            raw = raw[0].upper() + raw[1:]
        return f"{prefix}:{raw}"

    def _title_batches(self, lang, titles, params):
        """Bound both title count and the actual percent-encoded GET URL.

        Cyrillic and other non-ASCII titles expand substantially in a URL.
        Leave room below proxy limits for MediaWiki continuation parameters.
        """
        url = self.api._build_api_url(self.family, lang)
        batch = []
        for title in titles:
            trial = batch + [title]
            query = {"format": "json", "formatversion": 2, **params, "titles": "|".join(trial)}
            encoded_url = requests.Request("GET", url, params=query).prepare().url
            if batch and (len(trial) > self._MAX_PAGE_BATCH
                          or len(encoded_url.encode("ascii")) > self._MAX_QUERY_URL_BYTES):
                yield batch
                batch = [title]
            else:
                batch = trial
        if batch:
            yield batch

    def pages(self, lang: str, titles: list[str], *, props: str = "info|pageprops|langlinks|categoryinfo") -> dict[str, dict]:
        result = {}
        base_params = {"action": "query", "prop": props}
        if "revisions" in props:
            base_params.update(rvprop="content", rvslots="main")
        if "langlinks" in props:
            base_params.update(lllang=self.target_lang, lllimit="max")
        for batch in self._title_batches(lang, titles, base_params):
            params = {**base_params, "titles": "|".join(batch)}
            aliases = {}
            while True:
                data = self.request(lang, params)
                query = data.get("query", {})
                for alias in query.get("normalized", []):
                    aliases[alias["from"]] = alias["to"]
                for page in query.get("pages", []):
                    title = page["title"]
                    if title not in result:
                        result[title] = dict(page)
                    else:
                        result[title].setdefault("langlinks", []).extend(page.get("langlinks", []))
                if not data.get("continue"):
                    break
                params.update(data["continue"])
            for raw in batch:
                actual = aliases.get(raw, raw)
                if actual in result:
                    result[raw] = result[actual]
        return result

    @staticmethod
    def valid_page(page: dict | None, namespace: int) -> bool:
        return bool(page and "missing" not in page and "invalid" not in page
                    and "redirect" not in page and page.get("ns") == namespace)

    def members(self, category: str, kind: str) -> list[str]:
        params = {"action": "query", "list": "categorymembers", "cmtitle": category,
                  "cmtype": kind, "cmnamespace": 14 if kind == "subcat" else 0,
                  "cmlimit": "max"}
        titles = {}
        while True:
            data = self.request(self.source_lang, params)
            for page in data.get("query", {}).get("categorymembers", []):
                titles[page["title"]] = None
            if not data.get("continue"):
                return list(titles)
            params.update(data["continue"])

    def item_links(self, qids: list[str]) -> dict[str, str]:
        wiki_id = self.site_info(self.target_lang)["general"]["wikiid"]
        missing = list(dict.fromkeys(qid for qid in qids if qid not in self._item_links))
        for offset in range(0, len(missing), 50):
            batch = missing[offset:offset + 50]
            data = self.request("", {"action": "wbgetentities", "ids": "|".join(batch),
                                    "props": "sitelinks", "sitefilter": wiki_id}, wikidata=True)
            for qid in batch:
                item = data.get("entities", {}).get(qid, {})
                self._item_links[qid] = item.get("sitelinks", {}).get(wiki_id, {}).get("title", "")
        return {qid: self._item_links.get(qid, "") for qid in qids}

    def category_metadata(self, titles: list[str]) -> dict[str, dict]:
        pages = self.pages(self.source_lang, titles)
        qids = [page.get("pageprops", {}).get("wikibase_item", "")
                for page in pages.values()]
        links = self.item_links([qid for qid in qids if qid])
        candidates = {}
        for raw in titles:
            page = pages.get(raw)
            if not self.valid_page(page, 14):
                continue
            qid = page.get("pageprops", {}).get("wikibase_item", "")
            linked = links.get(qid, "")
            if not linked:
                linked = next((ll.get("title", "") for ll in page.get("langlinks", [])
                               if ll.get("lang") == self.target_lang), "")
            candidates[raw] = {"source_title": page["title"], "target_title": linked,
                               "wikidata_target": links.get(qid, ""),
                               "qid": qid, "total": None, "potential": None,
                               "status": "loaded", "target_articles": []}
        targets = [row["target_title"] for row in candidates.values() if row["target_title"]]
        verified = self.pages(self.target_lang, list(dict.fromkeys(targets)), props="info") if targets else {}
        for row in candidates.values():
            target = verified.get(row["target_title"])
            row["target_title"] = target["title"] if self.valid_page(target, 14) else ""
        return candidates

    def category_items(self, lang: str, pages: dict[str, dict]) -> None:
        """Fill pageprops lagging behind a newly written Wikidata sitelink."""
        missing = {page["title"]: page for page in pages.values()
                   if self.valid_page(page, 14) and not page.get("pageprops", {}).get("wikibase_item")}
        if not missing:
            return
        wiki = self.site_info(lang)["general"]["wikiid"]
        for titles in self._title_batches(lang, list(missing), {"action": "wbgetentities", "sites": wiki}):
            data = self.request("", {"action": "wbgetentities", "sites": wiki, "titles": "|".join(titles),
                                     "props": "sitelinks", "sitefilter": wiki}, wikidata=True)
            for entity in data.get("entities", {}).values():
                if "missing" in entity or not entity.get("id"):
                    continue
                linked = entity.get("sitelinks", {}).get(wiki, {}).get("title", "")
                title = self.category_title(linked, lang) if linked else ""
                if title in missing:
                    missing[title].setdefault("pageprops", {})["wikibase_item"] = entity["id"]

    def preview_creation(self, rows: tuple[CategoryCreationRow, ...], on_row, *, check_labels=False) -> list[dict]:
        """Inventory existing pages and links before enabling writes or resuming."""
        normalized = []
        sources, targets = set(), set()
        for index, row in enumerate(rows):
            source = self.category_title(row.source_title, self.source_lang)
            target = self.category_title(row.target_title, self.target_lang) if row.target_title.strip() else ""
            if source in sources or (target and target in targets):
                raise ValueError(message("tsv_duplicate", line=index + 1))
            sources.add(source)
            if target:
                targets.add(target)
            normalized.append(CategoryCreationRow(source, target, row.content))
        source_pages = self.pages(self.source_lang, list(sources), props="info|pageprops")
        self.category_items(self.source_lang, source_pages)
        qids = [page.get("pageprops", {}).get("wikibase_item", "") for page in source_pages.values()]
        links = self.item_links([qid for qid in qids if qid])
        # A linked local title needs no manual translation, even when entering
        # this stage directly with a list of foreign categories.
        for index, row in enumerate(normalized):
            if row.target_title:
                continue
            qid = source_pages.get(row.source_title, {}).get("pageprops", {}).get("wikibase_item", "")
            linked = links.get(qid, "")
            if linked:
                target = self.category_title(linked, self.target_lang)
                if target in targets:
                    raise ValueError(message("tsv_duplicate", line=index + 1))
                targets.add(target)
                normalized[index] = CategoryCreationRow(row.source_title, target, row.content)
        target_pages = self.pages(self.target_lang, list(targets), props="info|pageprops") if targets else {}
        self.category_items(self.target_lang, target_pages)
        entities = {}
        if check_labels:
            item_ids = list(dict.fromkeys(page.get("pageprops", {}).get("wikibase_item")
                                         for page in (*source_pages.values(), *target_pages.values())
                                         if page.get("pageprops", {}).get("wikibase_item")))
            for offset in range(0, len(item_ids), 50):
                data = self.request("", {"action": "wbgetentities", "ids": "|".join(item_ids[offset:offset + 50]),
                                         "props": "labels", "languages": f"{self.source_lang}|{self.target_lang}"},
                                    wikidata=True)
                entities.update(data.get("entities", {}))
        results = []
        for index, row in enumerate(normalized):
            self.check_stop()
            result = {"index": index, "row": row}
            source = source_pages.get(row.source_title)
            target = target_pages.get(row.target_title)
            source_qid = (source or {}).get("pageprops", {}).get("wikibase_item", "")
            target_qid = (target or {}).get("pageprops", {}).get("wikibase_item", "")
            result.update(source_qid=source_qid, target_qid=target_qid, qid=source_qid or target_qid)
            try:
                if not self.valid_page(source, 14):
                    raise ValueError(message("source_missing", title=row.source_title))
                if not row.target_title:
                    result.update(status="needs_translation", detail=message("translation_required"))
                    results.append(result)
                    on_row(result)
                    continue
                if target and "redirect" in target:
                    raise ValueError(message("target_redirect", title=row.target_title))
                if target and "invalid" in target:
                    raise ValueError(message("invalid_title", title=row.target_title))
                linked = links.get(source_qid, "")
                if linked and linked != row.target_title:
                    raise ValueError(message("sitelink_conflict", title=row.target_title, linked=linked))
                if source_qid and target_qid and source_qid != target_qid:
                    raise ValueError(message("item_conflict", source=row.source_title, target=row.target_title))
                if target_qid and not source_qid:
                    source_wiki = self.site_info(self.source_lang)["general"]["wikiid"]
                    data = self.request("", {"action": "wbgetentities", "ids": target_qid,
                                             "props": "sitelinks", "sitefilter": source_wiki}, wikidata=True)
                    source_link = data.get("entities", {}).get(target_qid, {}).get("sitelinks", {}).get(source_wiki, {}).get("title", "")
                    if source_link and source_link != row.source_title:
                        raise ValueError(message("sitelink_conflict", title=row.source_title, linked=source_link))
                if self.valid_page(target, 14):
                    result["status"] = "already_linked" if source_qid and source_qid == target_qid else "ready_link"
                    if check_labels and result["status"] == "already_linked":
                        entity = entities.get(source_qid, {})
                        if not entity or "missing" in entity:
                            raise APIRequestError(message("item_missing", qid=source_qid))
                        labels = entity.get("labels", {})
                        for lang, title in ((self.source_lang, row.source_title), (self.target_lang, row.target_title)):
                            label = labels.get(lang, {}).get("value", "")
                            if not label or label == title.split(":", 1)[1]:
                                result.update(status="ready_labels", detail=message("labels_required"))
                                break
                else:
                    if not row.content.strip():
                        result.update(status="needs_content", detail=message("tsv_content_required", line=index + 1))
                    else:
                        result["status"] = "ready_create"
            except (ValueError, APIRequestError) as error:
                result.update(status="error", error=str(error))
            results.append(result)
            on_row(result)
        return results

    def preview_interwiki(self, rows: tuple[CategoryCreationRow, ...], on_row) -> list[dict]:
        """Read existing category pairs and retain both item IDs for conflicts."""
        normalized, sources, targets = [], set(), set()
        for index, row in enumerate(rows):
            source = self.category_title(row.source_title, self.source_lang)
            target = self.category_title(row.target_title, self.target_lang) if row.target_title.strip() else ""
            if source in sources or (target and target in targets):
                raise ValueError(message("tsv_duplicate", line=index + 1))
            sources.add(source)
            if target:
                targets.add(target)
            normalized.append(CategoryCreationRow(source, target, ""))
        source_pages = self.pages(self.source_lang, list(sources), props="info|pageprops")
        target_pages = self.pages(self.target_lang, list(targets), props="info|pageprops") if targets else {}
        self.category_items(self.source_lang, source_pages)
        self.category_items(self.target_lang, target_pages)
        qids = list(dict.fromkeys(page.get("pageprops", {}).get("wikibase_item")
                                for page in (*source_pages.values(), *target_pages.values())
                                if page.get("pageprops", {}).get("wikibase_item")))
        source_wiki = self.site_info(self.source_lang)["general"]["wikiid"]
        target_wiki = self.site_info(self.target_lang)["general"]["wikiid"]
        entities = {}
        for offset in range(0, len(qids), 50):
            data = self.request("", {"action": "wbgetentities", "ids": "|".join(qids[offset:offset + 50]),
                                     "props": "sitelinks", "sitefilter": f"{source_wiki}|{target_wiki}"}, wikidata=True)
            entities.update(data.get("entities", {}))
        results = []
        for index, row in enumerate(normalized):
            self.check_stop()
            source, target = source_pages.get(row.source_title), target_pages.get(row.target_title)
            source_qid = (source or {}).get("pageprops", {}).get("wikibase_item", "")
            target_qid = (target or {}).get("pageprops", {}).get("wikibase_item", "")
            result = dict(index=index, row=row, source_qid=source_qid, target_qid=target_qid)
            try:
                if not self.valid_page(source, 14):
                    raise ValueError(message("source_missing", title=row.source_title))
                if not row.target_title:
                    result.update(status="needs_translation")
                elif not self.valid_page(target, 14):
                    raise ValueError(message("existing_target_required", title=row.target_title))
                elif source_qid and target_qid and source_qid != target_qid:
                    result.update(status="different_items")
                elif source_qid and source_qid == target_qid:
                    result.update(status="already_linked")
                else:
                    qid = source_qid or target_qid
                    entity = entities.get(qid, {})
                    if qid and (not entity or "missing" in entity):
                        raise APIRequestError(message("item_missing", qid=qid))
                    for wiki, title in ((source_wiki, row.source_title), (target_wiki, row.target_title)):
                        linked = entity.get("sitelinks", {}).get(wiki, {}).get("title", "")
                        if linked and linked.replace("_", " ") != title.replace("_", " "):
                            raise ValueError(message("sitelink_conflict", title=title, linked=linked))
                    result.update(status="ready_link" if qid else "ready_item")
            except (ValueError, APIRequestError) as error:
                result.update(status="error", error=str(error))
            results.append(result)
            on_row(result)
        return results

    def check_pairs(self, pairs: tuple[CategoryPair, ...]) -> list[dict]:
        """Recheck handed-off links; a changed or missing link cannot fall back."""
        rows = tuple(CategoryCreationRow(pair.source_title, pair.target_title, "") for pair in pairs)
        results = self.preview_creation(rows, lambda _result: None)
        for pair, result in zip(pairs, results):
            if result["status"] != "already_linked" or (pair.qid and pair.qid != result.get("qid")):
                result.update(status="error", error=result.get("error") or message("pair_changed"))
        return results

    def discover(self, category: str, depth: int, skip_existing_level: int,
                 include_root: bool, on_row, on_progress) -> list[dict]:
        root = self.category_title(category, self.source_lang)
        metadata = self.category_metadata([root])
        if root not in metadata:
            raise ValueError(message("source_missing", title=root))
        queue = deque([(root, 0)])
        seen = {root}
        rows = []
        if include_root:
            row = {**metadata[root], "depth": 0}
            rows.append(row)
            on_row(row)
        while queue:
            self.check_stop()
            title, level = queue.popleft()
            on_progress(title, len(rows))
            if level >= depth:
                continue
            children = [child for child in self.members(title, "subcat") if child not in seen]
            seen.update(children)
            child_metadata = self.category_metadata(children) if children else {}
            for child in children:
                row = child_metadata.get(child)
                if row is None:
                    continue
                row = {**row, "depth": level + 1}
                if skip_existing_level and level + 1 >= skip_existing_level and row["target_title"]:
                    continue
                rows.append(row)
                on_row(row)
                queue.append((row["source_title"], level + 1))
        return rows

    def article_matches(self, articles: list[str]) -> dict[str, str]:
        """Existing mainspace counterparts, shared by discovery and sync."""
        self.check_stop()
        uncached = [title for title in articles if title not in self._article_links]
        pages = self.pages(self.source_lang, uncached, props="info|pageprops") if uncached else {}
        qids = [page.get("pageprops", {}).get("wikibase_item", "") for page in pages.values()
                if self.valid_page(page, 0)]
        links = self.item_links([qid for qid in qids if qid])
        for title in uncached:
            page = pages.get(title)
            qid = page.get("pageprops", {}).get("wikibase_item", "") if self.valid_page(page, 0) else ""
            self._article_links[title] = links.get(qid, "")
        linked = list(dict.fromkeys(self._article_links[title] for title in articles
                                   if self._article_links.get(title)))
        targets = self.pages(self.target_lang, linked, props="info") if linked else {}
        return {title: targets[linked]["title"] for title in articles
                if (linked := self._article_links.get(title)) and self.valid_page(targets.get(linked), 0)}

    def scan(self, row: dict) -> dict:
        self.check_stop()
        title = row["source_title"]
        metadata = self.category_metadata([title])
        if title not in metadata:
            raise ValueError(message("source_missing", title=title))
        articles = self.members(title, "page")
        found = list(dict.fromkeys(self.article_matches(articles).values()))
        return {**row, **metadata[row["source_title"]], "total": len(articles),
                "potential": len(found), "target_articles": found, "status": "scanned"}
