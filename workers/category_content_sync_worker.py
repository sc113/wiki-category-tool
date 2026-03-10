# -*- coding: utf-8 -*-
"""
Workers для синхронизации содержимого категорий между языковыми разделами.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote
from typing import Optional

import pywikibot
import requests
from PySide6.QtCore import QThread, Signal

from .base_worker import BaseWorker
from ..constants import REQUEST_HEADERS
from ..core.api_client import WikimediaAPIClient
from ..core.localization import translate_runtime
from ..core.namespace_manager import get_policy_prefix, strip_ns_prefix


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _t(key: str, default: str = "") -> str:
    return translate_runtime(key, default)


def _fmt(key: str, default: str = "", **kwargs) -> str:
    text = _t(key, default)
    try:
        return text.format(**kwargs)
    except Exception:
        return text


def _normalize_category_name(raw_name: str, family: str, lang: str) -> str:
    raw = (raw_name or "").strip().lstrip("\ufeff")
    if not raw:
        return ""

    if raw.startswith("[[") and raw.endswith("]]"):
        raw = raw[2:-2].strip()
    if "|" in raw:
        raw = raw.split("|", 1)[0].strip()

    base = strip_ns_prefix(family, lang, raw, 14)
    if not base:
        base = raw
    if ":" in base:
        base = base.split(":", 1)[-1]
    base = base.strip()
    if not base:
        return ""

    prefix = get_policy_prefix(family, lang, 14, "Category:")
    return f"{prefix}{base}"


def _prepare_target_categories(
    target_categories: list[str], family: str, lang: str
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in target_categories:
        name = _normalize_category_name(raw, family, lang)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(name)
    return normalized


def _extract_wikidata_qid(page: pywikibot.Page) -> str:
    try:
        item_page = pywikibot.ItemPage.fromPage(page)
        qid = item_page.getID()
        return str(qid or "").strip()
    except Exception:
        return ""


def _get_linked_page_title(
    source_page: pywikibot.Page,
    target_site: pywikibot.Site,
    expected_ns: Optional[int] = None,
) -> Optional[str]:
    try:
        item_page = pywikibot.ItemPage.fromPage(source_page)
        linked_sitelink = item_page.getSitelink(target_site)
        if not linked_sitelink:
            return None

        linked_page = pywikibot.Page(target_site, linked_sitelink)
        if expected_ns is not None and linked_page.namespace().id != expected_ns:
            return None
        if linked_page.isRedirectPage():
            return None
        return linked_page.title()
    except pywikibot.exceptions.NoPageError:
        return None
    except pywikibot.exceptions.IsRedirectPageError:
        return None
    except Exception:
        return None


def _resolve_source_category(
    target_category_name: str,
    source_site: pywikibot.Site,
    target_site: pywikibot.Site,
    *,
    target_category_obj: Optional[pywikibot.Category] = None,
) -> tuple[Optional[str], str]:
    method = ""
    target_cat = target_category_obj
    if target_cat is None:
        try:
            target_cat = pywikibot.Category(target_site, target_category_name)
        except Exception:
            return None, method

    linked = _get_linked_page_title(target_cat, source_site, expected_ns=14)
    if linked:
        method = "Wikidata"
        try:
            source_cat = pywikibot.Category(source_site, linked)
            if source_cat.exists() and not source_cat.isRedirectPage():
                return source_cat.title(), method
        except Exception:
            pass

    # Fallback: локальный префикс исходного языка + базовое имя категории.
    try:
        base_name = target_cat.title(with_ns=False)
        constructed = pywikibot.Category(source_site, base_name)
        if constructed.exists() and not constructed.isRedirectPage():
            return constructed.title(), _t("ui.sync.method.constructed", "Constructed")
    except Exception:
        pass
    return None, method


def _count_articles_in_source_category(
    source_cat: pywikibot.Category, article_depth: int
) -> int:
    recurse_value = article_depth if article_depth > 0 else False
    seen: set[str] = set()
    count = 0
    for source_page in source_cat.articles(recurse=recurse_value):
        if isinstance(source_page, pywikibot.Category):
            continue
        key = source_page.title().casefold()
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def _count_subcategories_in_source_category(source_cat: pywikibot.Category) -> int:
    seen: set[str] = set()
    for subcat in source_cat.subcategories():
        seen.add(subcat.title().casefold())
    return len(seen)


def _make_member_count_text(
    source_site: pywikibot.Site,
    source_category_name: str,
    *,
    process_articles: bool,
    process_subcategories: bool,
    article_depth: int,
) -> str:
    try:
        source_cat = pywikibot.Category(source_site, source_category_name)
        if not source_cat.exists():
            return "0"
        if process_articles and not process_subcategories:
            return str(_count_articles_in_source_category(source_cat, article_depth))
        if process_subcategories and not process_articles:
            return str(_count_subcategories_in_source_category(source_cat))
        if process_articles and process_subcategories:
            art = _count_articles_in_source_category(source_cat, article_depth)
            sub = _count_subcategories_in_source_category(source_cat)
            return f"{art} / {sub}"
        return "0"
    except Exception:
        return "0"


def _build_page_url(site: pywikibot.Site, title: str) -> str:
    try:
        host = site.hostname()
    except Exception:
        try:
            host = f"{site.code}.{site.family.name}.org"
        except Exception:
            host = "www.wikipedia.org"
    safe_title = quote((title or "").replace(" ", "_"))
    return f"https://{host}/wiki/{safe_title}"


def _format_sync_summary(template: str, *, context: dict[str, str]) -> str:
    """Подставляет поддерживаемые переменные {name} в шаблон комментария."""
    if not template:
        return ""
    result = str(template)
    for key, value in (context or {}).items():
        result = result.replace("{" + str(key) + "}", str(value or ""))
    return result.strip()


class CategoryContentSyncPreviewWorker(QThread):
    """Worker предпросмотра сопоставлений и объёмов перед переносом."""

    progress = Signal(str)
    rows_ready = Signal(object)
    category_done = Signal()

    def __init__(
        self,
        *,
        target_categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        process_articles: bool,
        process_subcategories: bool,
        article_depth: int,
    ):
        super().__init__()
        self.target_categories = list(target_categories or [])
        self.source_lang = (source_lang or "").strip().lower()
        self.target_lang = (target_lang or "").strip().lower()
        self.family = (family or "wikipedia").strip()
        self.process_articles = bool(process_articles)
        self.process_subcategories = bool(process_subcategories)
        self.article_depth = max(0, int(article_depth or 0))
        self.preview_batch_size = 50
        self.preview_api_batch_size = 50
        self.preview_api_retries = 3
        self.preview_api_timeout = 4
        self.api_client = WikimediaAPIClient()
        self._stop = False

    def request_stop(self):
        self._stop = True

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int):
        step = max(1, int(chunk_size or 1))
        for i in range(0, len(values), step):
            yield values[i : i + step]

    @staticmethod
    def _site_api_url(site: pywikibot.Site) -> str:
        try:
            host = site.hostname()
        except Exception:
            try:
                host = f"{site.code}.{site.family.name}.org"
            except Exception:
                host = "www.wikipedia.org"
        return f"https://{host}/w/api.php"

    def _source_sitelink_key(self) -> str:
        family = (self.family or "").strip().lower()
        lang = (self.source_lang or "").strip().lower()
        if family == "wikipedia":
            return f"{lang}wiki"
        if family == "commons":
            return "commonswiki"
        if family == "wikidata":
            return "wikidatawiki"
        return f"{lang}{family}"

    def _api_get_json(self, url: str, params: dict) -> tuple[dict, int]:
        attempts = max(1, int(self.preview_api_retries or 1))
        timeout = max(3, int(self.preview_api_timeout or 8))
        for attempt in range(1, attempts + 1):
            if self._stop:
                return {}, 0
            try:
                self.api_client._rate_wait()
            except Exception:
                pass
            try:
                response = self.api_client.session.post(
                    url,
                    data=params,
                    timeout=(3, timeout),
                    headers=REQUEST_HEADERS,
                )
                status = int(response.status_code or 0)
            except requests.exceptions.Timeout:
                status = 0
                if attempt < attempts:
                    try:
                        self.api_client._rate_backoff(0.35 * attempt)
                    except Exception:
                        pass
                    continue
                return {}, status
            except Exception:
                status = 0
                if attempt < attempts:
                    try:
                        self.api_client._rate_backoff(0.25 * attempt)
                    except Exception:
                        pass
                    continue
                return {}, status

            if self._stop:
                return {}, status

            if status == 200:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    return data, status
                return {}, status

            if status == 429 and attempt < attempts:
                try:
                    self.api_client._rate_backoff(0.6 * attempt)
                except Exception:
                    pass
                if attempt == 1:
                    self.progress.emit("[Preview API] 429 (rate limit), retrying...")
                continue

            if status in (502, 503, 504, 520, 521, 522, 524) and attempt < attempts:
                try:
                    self.api_client._rate_backoff(0.45 * attempt)
                except Exception:
                    pass
                continue

            return {}, status
        return {}, 0

    def _build_preview_rows_batch(
        self,
        *,
        batch_titles: list[str],
        source_site: pywikibot.Site,
        target_site: pywikibot.Site,
    ) -> list[dict]:
        if not batch_titles:
            return []
        if self._stop:
            return []

        target_api = self._site_api_url(target_site)
        source_api = self._site_api_url(source_site)
        requested_keys = {t.casefold() for t in batch_titles}
        qid_by_row_key: dict[str, str] = {}

        target_data, target_status = self._api_get_json(
            target_api,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "titles": "|".join(batch_titles),
            },
        )
        if target_status in (413, 414) and len(batch_titles) > 1:
            mid = len(batch_titles) // 2
            left = self._build_preview_rows_batch(
                batch_titles=batch_titles[:mid],
                source_site=source_site,
                target_site=target_site,
            )
            right = self._build_preview_rows_batch(
                batch_titles=batch_titles[mid:],
                source_site=source_site,
                target_site=target_site,
            )
            return left + right
        pages = ((target_data.get("query") or {}).get("pages") or [])
        if not pages and target_status and target_status != 200:
            self.progress.emit(
                f"[Preview API] target query HTTP {target_status}, batch={len(batch_titles)}"
            )
        for page in pages:
            if self._stop:
                break
            if not isinstance(page, dict):
                continue
            if page.get("missing"):
                continue
            title = str(page.get("title") or "").strip()
            if not title:
                continue
            key = title.casefold()
            if key not in requested_keys:
                continue
            qid = str(((page.get("pageprops") or {}).get("wikibase_item") or "")).strip()
            if qid:
                qid_by_row_key[key] = qid

        source_title_by_row_key: dict[str, str] = {}
        method_by_row_key: dict[str, str] = {}

        qids = sorted({qid for qid in qid_by_row_key.values() if qid})
        if qids and not self._stop:
            sitelink_key = self._source_sitelink_key()
            wd_api = "https://www.wikidata.org/w/api.php"
            def _fetch_sitelinks(qid_list: list[str]) -> dict[str, str]:
                if not qid_list or self._stop:
                    return {}
                entities_data, status = self._api_get_json(
                    wd_api,
                    {
                        "action": "wbgetentities",
                        "format": "json",
                        "formatversion": "2",
                        "ids": "|".join(qid_list),
                        "props": "sitelinks",
                    },
                )
                if status in (413, 414) and len(qid_list) > 1:
                    mid = len(qid_list) // 2
                    out: dict[str, str] = {}
                    out.update(_fetch_sitelinks(qid_list[:mid]))
                    out.update(_fetch_sitelinks(qid_list[mid:]))
                    return out
                if status and status != 200 and status not in (0,):
                    self.progress.emit(
                        f"[Preview API] wbgetentities HTTP {status}, batch={len(qid_list)}"
                    )
                entities = entities_data.get("entities") or {}
                if isinstance(entities, dict):
                    iterable = entities.items()
                elif isinstance(entities, list):
                    iterable = [
                        (str(entity.get("id") or ""), entity)
                        for entity in entities
                        if isinstance(entity, dict)
                    ]
                else:
                    iterable = []
                titles_by_qid_local: dict[str, str] = {}
                for ent_id, entity in iterable:
                    if not isinstance(entity, dict):
                        continue
                    sl = ((entity.get("sitelinks") or {}).get(sitelink_key) or {})
                    src_title = str(sl.get("title") or "").strip()
                    if src_title and ent_id:
                        titles_by_qid_local[str(ent_id).strip().upper()] = src_title
                return titles_by_qid_local

            titles_by_qid: dict[str, str] = {}
            for q_chunk in self._iter_chunks(qids, self.preview_api_batch_size):
                if self._stop:
                    break
                titles_by_qid.update(_fetch_sitelinks(q_chunk))

            for row_key, qid in qid_by_row_key.items():
                if row_key in source_title_by_row_key:
                    continue
                src = titles_by_qid.get(str(qid).strip().upper(), "")
                if src:
                    source_title_by_row_key[row_key] = src
                    method_by_row_key[row_key] = "Wikidata"

        # Fallback-конструирование для строк без sitelink.
        source_cat_prefix = get_policy_prefix(self.family, self.source_lang, 14, "Category:")
        constructed_by_row_key: dict[str, str] = {}
        for target_title in batch_titles:
            if self._stop:
                break
            row_key = target_title.casefold()
            if source_title_by_row_key.get(row_key):
                continue
            base = strip_ns_prefix(self.family, self.target_lang, target_title, 14)
            if not base:
                base = target_title.split(":", 1)[-1].strip()
            if not base:
                continue
            constructed = f"{source_cat_prefix}{base}"
            constructed_by_row_key[row_key] = constructed

        if constructed_by_row_key and not self._stop:
            constructed_titles = list(constructed_by_row_key.values())
            exists_keys: set[str] = set()

            def _fetch_existing_categories(title_list: list[str]) -> set[str]:
                if not title_list or self._stop:
                    return set()
                src_data, status = self._api_get_json(
                    source_api,
                    {
                        "action": "query",
                        "format": "json",
                        "formatversion": "2",
                        "titles": "|".join(title_list),
                    },
                )
                if status in (413, 414) and len(title_list) > 1:
                    mid = len(title_list) // 2
                    return _fetch_existing_categories(
                        title_list[:mid]
                    ) | _fetch_existing_categories(title_list[mid:])
                if status and status != 200 and status not in (0,):
                    self.progress.emit(
                        f"[Preview API] source query HTTP {status}, batch={len(title_list)}"
                    )
                found: set[str] = set()
                src_pages = ((src_data.get("query") or {}).get("pages") or [])
                for src_page in src_pages:
                    if not isinstance(src_page, dict):
                        continue
                    if src_page.get("missing"):
                        continue
                    if int(src_page.get("ns", -1)) != 14:
                        continue
                    if src_page.get("redirect"):
                        continue
                    page_title = str(src_page.get("title") or "").strip()
                    if page_title:
                        found.add(page_title.casefold())
                return found

            for c_chunk in self._iter_chunks(constructed_titles, self.preview_api_batch_size):
                if self._stop:
                    break
                exists_keys |= _fetch_existing_categories(c_chunk)
            for row_key, constructed in constructed_by_row_key.items():
                if constructed.casefold() in exists_keys:
                    source_title_by_row_key[row_key] = constructed
                    method_by_row_key[row_key] = _t("ui.sync.method.constructed", "Constructed")

        rows: list[dict] = []
        for target_category_name in batch_titles:
            row_key = target_category_name.casefold()
            qid = qid_by_row_key.get(row_key, "")
            source_title = source_title_by_row_key.get(row_key, "")
            rows.append(
                {
                    "phase": "details",
                    "row_key": row_key,
                    "target_title": target_category_name,
                    "target_url": _build_page_url(target_site, target_category_name),
                    "qid": qid,
                    "qid_url": f"https://www.wikidata.org/wiki/{qid}" if qid else "",
                    "source_title": source_title,
                    "source_url": _build_page_url(source_site, source_title)
                    if source_title
                    else "",
                    "count_text": "—",
                    "method": method_by_row_key.get(row_key, ""),
                }
            )
        return rows

    def run(self):
        if not self.process_articles and not self.process_subcategories:
            self.progress.emit(
                _t("log.sync_preview.no_modes_selected")
            )
            return
        if not self.source_lang or not self.target_lang:
            self.progress.emit(
                _t("log.sync_preview.missing_languages")
            )
            return
        if self.source_lang == self.target_lang:
            self.progress.emit(
                _t("log.sync_preview.same_languages")
            )
            return

        normalized_targets = _prepare_target_categories(
            self.target_categories, self.family, self.target_lang
        )
        if not normalized_targets:
            self.progress.emit(_t("log.sync_preview.empty_categories"))
            return

        try:
            source_site = pywikibot.Site(self.source_lang, self.family)
            target_site = pywikibot.Site(self.target_lang, self.family)
        except Exception as exc:
            self.progress.emit(
                _fmt("log.sync_preview.site_init_error", error_type=type(exc).__name__, error=exc)
            )
            return

        self.progress.emit(
            _fmt(
                "log.sync_preview.started",
                source=self.source_lang,
                target=self.target_lang,
                family=self.family,
                categories=len(normalized_targets),
            )
        )
        total = len(normalized_targets)
        pending_rows: list[dict] = []
        processed_count = 0

        def _flush_pending_rows():
            if not pending_rows:
                return
            self.rows_ready.emit(list(pending_rows))
            pending_rows.clear()

        for batch_titles in self._iter_chunks(
            normalized_targets, self.preview_api_batch_size
        ):
            if self._stop:
                break
            batch_rows = self._build_preview_rows_batch(
                batch_titles=batch_titles,
                source_site=source_site,
                target_site=target_site,
            )
            for row_data in batch_rows:
                if self._stop:
                    break
                pending_rows.append(row_data)
                processed_count += 1
                if len(pending_rows) >= self.preview_batch_size:
                    _flush_pending_rows()
                try:
                    self.category_done.emit()
                except Exception:
                    pass
            if self._stop:
                break
            if processed_count % self.preview_api_batch_size == 0 or processed_count == total:
                self.progress.emit(
                    _fmt("log.sync_preview.processed", processed=processed_count, total=total)
                )

        _flush_pending_rows()

        if not self._stop:
            self.progress.emit(_t("log.sync_preview.completed"))


class CategoryContentSyncWorker(BaseWorker):
    """Worker переноса содержимого категорий между языками в рамках одного family."""

    category_done = Signal()
    category_result = Signal(dict)
    member_progress = Signal(dict)

    def __init__(
        self,
        *,
        target_categories: list[str],
        username: str,
        password: str,
        source_lang: str,
        target_lang: str,
        family: str,
        process_articles: bool,
        process_subcategories: bool,
        article_depth: int,
        edit_summary: str = "",
    ):
        super().__init__(username, password, target_lang, family)
        self.target_categories = list(target_categories or [])
        self.source_lang = (source_lang or "").strip().lower()
        self.target_lang = (target_lang or "").strip().lower()
        self.family = (family or "wikipedia").strip()
        self.process_articles = bool(process_articles)
        self.process_subcategories = bool(process_subcategories)
        self.article_depth = max(0, int(article_depth or 0))
        self.edit_summary = str(edit_summary or "").strip()
        self.transferred_success_total = 0
        self._lookup_min_interval = 0.12
        self._lookup_last_ts = 0.0
        self._lookup_retries = 4
        self._target_category_prefix = get_policy_prefix(
            self.family, self.target_lang, 14, "Category:"
        )

    def run(self):
        if not self.process_articles and not self.process_subcategories:
            self.progress.emit(
                self._t("log.sync_worker.no_modes_selected")
            )
            return
        if not self.source_lang or not self.target_lang:
            self.progress.emit(self._t("log.sync_worker.missing_languages"))
            return
        if self.source_lang == self.target_lang:
            self.progress.emit(self._t("log.sync_worker.same_languages"))
            return

        normalized_targets = _prepare_target_categories(
            self.target_categories, self.family, self.target_lang
        )
        if not normalized_targets:
            self.progress.emit(
                self._t("log.sync_worker.no_valid_targets")
            )
            return

        try:
            source_site = pywikibot.Site(self.source_lang, self.family)
            target_site = pywikibot.Site(self.target_lang, self.family)
        except Exception as exc:
            self.progress.emit(
                self._fmt("log.sync_worker.site_init_error", error_type=type(exc).__name__, error=exc)
            )
            return

        # Для редактирования вход обязателен только на целевом сайте.
        if self.username and self.password:
            try:
                target_site.login(user=self.username)
            except Exception as exc:
                self.progress.emit(
                    self._fmt(
                        "log.sync_worker.target_auth_error",
                        site=f"{target_site.code}.{target_site.family.name}",
                        error_type=type(exc).__name__,
                        error=exc,
                    )
                )
                return
            try:
                source_site.login(user=self.username)
            except Exception as exc:
                self.progress.emit(
                    self._fmt(
                        "log.sync_worker.source_auth_warning",
                        site=f"{source_site.code}.{source_site.family.name}",
                        error_type=type(exc).__name__,
                        error=exc,
                    )
                )

        total_requested = len(normalized_targets)
        total_processed_pairs = 0
        total_failed_source = 0
        stats_articles = {"success": 0, "error": 0, "skipped": 0, "already": 0}
        stats_subcats = {"success": 0, "error": 0, "skipped": 0, "already": 0}
        self.transferred_success_total = 0

        self.progress.emit(
            self._fmt(
                "log.sync_worker.started",
                source=self.source_lang,
                target=self.target_lang,
                family=self.family,
            )
        )
        self.progress.emit(
            self._fmt(
                "log.sync_worker.categories_to_process",
                total=total_requested,
                articles=self.process_articles,
                subcategories=self.process_subcategories,
            )
        )

        for idx, target_category_name in enumerate(normalized_targets, start=1):
            if self._stop:
                break
            row_key = target_category_name.casefold()
            self._emit_category_result(
                target_category_name,
                row_key=row_key,
                status="in_progress",
                transferred=0,
                # Не затираем значение "Всего в категории", полученное на этапе предпросмотра.
                total_text="",
            )
            self.progress.emit(
                self._fmt(
                    "log.sync_worker.processing_header",
                    index=idx,
                    total=total_requested,
                    category=target_category_name,
                )
            )
            source_category_name, source_method = self._resolve_source_category_with_retry(
                target_category_name, source_site, target_site
            )
            if not source_category_name:
                total_failed_source += 1
                self.progress.emit(
                    self._fmt("log.sync_worker.source_not_resolved", category=target_category_name)
                )
                self._emit_category_result(
                    target_category_name,
                    row_key=row_key,
                    status="skipped",
                    transferred=0,
                    total_text="0",
                )
                self._emit_category_done()
                continue

            total_processed_pairs += 1
            self.progress.emit(
                self._fmt(
                    "log.sync_worker.source_found",
                    method=source_method or "n/a",
                    category=source_category_name,
                )
            )
            transferred_total = 0
            errors_total = 0
            seen_articles = 0
            seen_subcats = 0
            category_processed = 0
            category_total = 0
            article_members: list[pywikibot.Page] = []
            subcategory_members: list[pywikibot.Category] = []
            try:
                source_cat_obj = pywikibot.Category(source_site, source_category_name)
                if self.process_articles:
                    article_members = self._collect_article_members(source_cat_obj)
                    seen_articles = len(article_members)
                if self.process_subcategories:
                    subcategory_members = self._collect_subcategory_members(source_cat_obj)
                    seen_subcats = len(subcategory_members)
            except Exception as exc:
                errors_total += 1
                self.progress.emit(
                    self._fmt("log.sync_worker.preparation_error", category=source_category_name, error=exc)
                )

            if self.process_articles and self.process_subcategories:
                total_text = f"{seen_articles} / {seen_subcats}"
                category_total = seen_articles + seen_subcats
            elif self.process_articles:
                total_text = str(seen_articles)
                category_total = seen_articles
            elif self.process_subcategories:
                total_text = str(seen_subcats)
                category_total = seen_subcats
            else:
                total_text = "0"
                category_total = 0

            # Сразу обновляем "Всего в категории" до начала обработки элементов.
            self._emit_category_result(
                target_category_name,
                row_key=row_key,
                status="in_progress",
                transferred=0,
                total_text=total_text,
            )
            self._emit_member_progress(
                row_key=row_key,
                target_title=target_category_name,
                processed=0,
                total=category_total,
                mode="category",
            )

            if self.process_articles:
                stats = self._transfer_articles(
                    source_category_name,
                    target_category_name,
                    source_site,
                    target_site,
                    source_members=article_members,
                    row_key=row_key,
                    processed_offset=category_processed,
                    category_total=category_total,
                )
                for key in stats_articles:
                    stats_articles[key] += stats.get(key, 0)
                transferred_total += int(stats.get("success", 0))
                errors_total += int(stats.get("error", 0))
                category_processed += seen_articles

            if self.process_subcategories:
                stats = self._transfer_subcategories(
                    source_category_name,
                    target_category_name,
                    source_site,
                    target_site,
                    source_members=subcategory_members,
                    row_key=row_key,
                    processed_offset=category_processed,
                    category_total=category_total,
                )
                for key in stats_subcats:
                    stats_subcats[key] += stats.get(key, 0)
                transferred_total += int(stats.get("success", 0))
                errors_total += int(stats.get("error", 0))
                category_processed += seen_subcats
            self.transferred_success_total = int(
                getattr(self, "transferred_success_total", 0) or 0
            ) + max(0, int(transferred_total or 0))

            status = "done"
            if transferred_total == 0 and errors_total > 0:
                status = "error"
            elif transferred_total > 0 and errors_total > 0:
                status = "partial"
            elif transferred_total == 0:
                status = "done_empty"
            self._emit_category_result(
                target_category_name,
                row_key=row_key,
                status=status,
                transferred=transferred_total,
                total_text=total_text,
            )

            self._emit_category_done()

        if self._stop:
            self.progress.emit(self._t("log.sync_worker.stopped"))
        else:
            self.progress.emit(self._t("log.sync_worker.completed"))

        self.progress.emit(
            self._fmt(
                "log.sync_worker.summary_categories",
                requested=total_requested,
                processed=total_processed_pairs,
                missing_source=total_failed_source,
            )
        )
        if self.process_articles:
            self.progress.emit(
                self._fmt(
                    "log.sync_worker.summary_articles",
                    success=stats_articles["success"],
                    errors=stats_articles["error"],
                    skipped=stats_articles["skipped"],
                    already=stats_articles["already"],
                )
            )
        if self.process_subcategories:
            self.progress.emit(
                self._fmt(
                    "log.sync_worker.summary_subcategories",
                    success=stats_subcats["success"],
                    errors=stats_subcats["error"],
                    skipped=stats_subcats["skipped"],
                    already=stats_subcats["already"],
                )
            )

    def _emit_category_done(self) -> None:
        try:
            self.category_done.emit()
        except Exception:
            pass

    def _emit_category_result(
        self,
        target_category_name: str,
        *,
        row_key: str,
        status: str,
        transferred: int,
        total_text: str = "",
    ) -> None:
        try:
            self.category_result.emit(
                {
                    "target_title": target_category_name,
                    "row_key": row_key,
                    "status": str(status or ""),
                    "transferred": int(transferred or 0),
                    "total_text": str(total_text or ""),
                }
            )
        except Exception:
            pass

    def _emit_member_progress(
        self,
        *,
        row_key: str,
        target_title: str,
        processed: int,
        total: Optional[int] = None,
        mode: str,
    ) -> None:
        try:
            self.member_progress.emit(
                {
                    "row_key": str(row_key or "").strip().casefold(),
                    "target_title": str(target_title or "").strip(),
                    "processed": max(0, int(processed or 0)),
                    "total": (
                        max(0, int(total))
                        if total is not None and str(total).strip() != ""
                        else None
                    ),
                    "mode": str(mode or "").strip().lower(),
                }
            )
        except Exception:
            pass

    def _category_already_exists(self, text: str, category_name: str) -> bool:
        normalized_text = _normalize_space(text)
        base_category_name = _normalize_space(category_name.split(":", 1)[-1])
        target_prefix_pattern = re.escape(
            (self._target_category_prefix or "Category:").rstrip(":")
        )
        category_tag_pattern = (
            rf"\[\[\s*{target_prefix_pattern}\s*:\s*"
            rf"{re.escape(base_category_name)}(?:\s*\|[^\]]*)?\s*\]\]"
        )
        return bool(re.search(category_tag_pattern, normalized_text, re.IGNORECASE))

    def _wait_before_lookup(self):
        now = time.time()
        wait_s = max(
            0.0,
            (
                float(getattr(self, "_lookup_last_ts", 0.0) or 0.0)
                + float(getattr(self, "_lookup_min_interval", 0.12) or 0.12)
            )
            - now,
        )
        if wait_s > 0:
            time.sleep(wait_s)
        self._lookup_last_ts = time.time()

    def _increase_lookup_interval(self, attempt: int, hinted_wait: float = 0.0):
        cur = float(getattr(self, "_lookup_min_interval", 0.12) or 0.12)
        target = max(
            cur * 1.45,
            0.25 * max(1, int(attempt or 1)),
            float(hinted_wait or 0.0),
        )
        self._lookup_min_interval = min(2.5, target)

    def _get_linked_page_title_retry(
        self,
        source_page: pywikibot.Page,
        target_site: pywikibot.Site,
        *,
        expected_ns: Optional[int] = None,
    ) -> Optional[str]:
        retries = max(1, int(getattr(self, "_lookup_retries", 4) or 4))
        for attempt in range(1, retries + 1):
            if self._stop:
                return None
            try:
                self._wait_before_lookup()
                item_page = pywikibot.ItemPage.fromPage(source_page)
                linked_sitelink = item_page.getSitelink(target_site)
                if not linked_sitelink:
                    return None
                linked_page = pywikibot.Page(target_site, linked_sitelink)
                if expected_ns is not None and linked_page.namespace().id != expected_ns:
                    return None
                if linked_page.isRedirectPage():
                    return None
                return linked_page.title()
            except pywikibot.exceptions.NoPageError:
                return None
            except pywikibot.exceptions.IsRedirectPageError:
                return None
            except Exception as exc:
                if self._is_rate_error(exc) and attempt < retries:
                    hinted = self._extract_wait_seconds(str(exc))
                    self._increase_lookup_interval(attempt, hinted_wait=hinted)
                    wait_s = max(
                        float(getattr(self, "_lookup_min_interval", 0.12) or 0.12),
                        float(hinted or 0.0),
                    )
                    try:
                        self.progress.emit(
                            self._fmt(
                                "log.sync_worker.read_rate_limit",
                                wait=wait_s,
                                attempt=attempt,
                                retries=retries,
                            )
                        )
                    except Exception:
                        pass
                    time.sleep(wait_s)
                    continue
                return None
        return None

    def _resolve_source_category_with_retry(
        self,
        target_category_name: str,
        source_site: pywikibot.Site,
        target_site: pywikibot.Site,
    ) -> tuple[Optional[str], str]:
        method = ""
        try:
            target_cat = pywikibot.Category(target_site, target_category_name)
        except Exception:
            return None, method

        linked = self._get_linked_page_title_retry(
            target_cat, source_site, expected_ns=14
        )
        if linked:
            method = "Wikidata"
            try:
                source_cat = pywikibot.Category(source_site, linked)
                if source_cat.exists() and not source_cat.isRedirectPage():
                    return source_cat.title(), method
            except Exception:
                pass

        try:
            base_name = target_cat.title(with_ns=False)
            constructed = pywikibot.Category(source_site, base_name)
            if constructed.exists() and not constructed.isRedirectPage():
                return constructed.title(), self._t("ui.sync.method.constructed", "Constructed")
        except Exception:
            pass
        return None, method

    def _build_edit_summary(
        self,
        *,
        mode: str,
        source_category_name: str,
        target_category_name: str,
        source_page_title: str = "",
        target_page_title: str = "",
    ) -> str:
        template = (self.edit_summary or "").strip()
        if not template:
            return ""
        mode_key = str(mode or "").strip().lower()
        target_action = self._t(
            "ui.sync.target_action.parent_category",
            "parent category",
        ) if mode_key == "subcategories" else self._t(
            "ui.sync.target_action.category",
            "category",
        )
        target_action_en = "parent category" if mode_key == "subcategories" else "category"
        source_base = (source_category_name or "").split(":", 1)[-1].strip()
        target_base = (target_category_name or "").split(":", 1)[-1].strip()
        context = {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "family": self.family,
            "mode": mode_key,
            "target_action": target_action,
            "target_action_en": target_action_en,
            "source_category": source_category_name or "",
            "target_category": target_category_name or "",
            "source_category_base": source_base,
            "target_category_base": target_base,
            "source_page": source_page_title or "",
            "target_page": target_page_title or "",
        }
        return _format_sync_summary(template, context=context)

    def _collect_article_members(
        self, source_cat: pywikibot.Category
    ) -> list[pywikibot.Page]:
        members: list[pywikibot.Page] = []
        seen: set[str] = set()
        recurse_value = self.article_depth if self.article_depth > 0 else False
        for source_page in source_cat.articles(recurse=recurse_value):
            if self._stop:
                break
            if isinstance(source_page, pywikibot.Category):
                continue
            key = source_page.title().casefold()
            if key in seen:
                continue
            seen.add(key)
            members.append(source_page)
        return members

    def _collect_subcategory_members(
        self, source_cat: pywikibot.Category
    ) -> list[pywikibot.Category]:
        members: list[pywikibot.Category] = []
        seen: set[str] = set()
        for source_subcat in source_cat.subcategories():
            if self._stop:
                break
            key = source_subcat.title().casefold()
            if key in seen:
                continue
            seen.add(key)
            members.append(source_subcat)
        return members

    def _transfer_articles(
        self,
        source_category_name: str,
        target_category_name: str,
        source_site: pywikibot.Site,
        target_site: pywikibot.Site,
        *,
        source_members: list[pywikibot.Page],
        row_key: str,
        processed_offset: int = 0,
        category_total: int = 0,
    ) -> dict[str, int]:
        stats = {"success": 0, "error": 0, "skipped": 0, "already": 0, "seen": 0}
        self.progress.emit(
            self._fmt(
                "log.sync_worker.articles_processing",
                source=source_category_name,
                target=target_category_name,
            )
        )
        try:
            stats["seen"] = len(source_members)
            article_count = len(source_members)
            for idx, source_page in enumerate(source_members, start=1):
                if self._stop:
                    break
                self._emit_member_progress(
                    row_key=row_key,
                    target_title=target_category_name,
                    processed=processed_offset + int(idx),
                    total=category_total,
                    mode="articles",
                )
                source_title = source_page.title()
                target_page_title = self._get_linked_page_title_retry(
                    source_page, target_site, expected_ns=0
                )
                if not target_page_title:
                    stats["skipped"] += 1
                    self.progress.emit(
                        self._fmt("log.sync_worker.article_skip_no_linked", title=source_title)
                    )
                    continue

                try:
                    target_page = pywikibot.Page(target_site, target_page_title)
                    if not target_page.exists():
                        stats["skipped"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.article_skip_missing", title=target_page.title())
                        )
                        continue
                    if target_page.isRedirectPage():
                        stats["skipped"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.article_skip_redirect", title=target_page.title())
                        )
                        continue

                    text = target_page.get()
                    if self._category_already_exists(text, target_category_name):
                        stats["already"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.article_already_in_category", title=target_page.title())
                        )
                        continue

                    new_text = text.rstrip() + f"\n[[{target_category_name}]]\n"
                    if self.edit_summary:
                        summary = self._build_edit_summary(
                            mode="articles",
                            source_category_name=source_category_name,
                            target_category_name=target_category_name,
                            source_page_title=source_title,
                            target_page_title=target_page.title(),
                        )
                    else:
                        summary = (
                            self._fmt(
                                "log.sync_worker.add_category_summary",
                                target=target_category_name,
                                source_code=source_site.code,
                                source_category=source_category_name,
                            )
                        )
                    if not summary:
                        summary = (
                            self._fmt(
                                "log.sync_worker.add_category_summary",
                                target=target_category_name,
                                source_code=source_site.code,
                                source_category=source_category_name,
                            )
                        )
                    ok = self._save_with_retry(target_page, new_text, summary, False)
                    if ok:
                        stats["success"] += 1
                        self.progress.emit(self._fmt("log.sync_worker.article_success", title=target_page.title()))
                    else:
                        stats["error"] += 1
                except Exception as exc:
                    stats["error"] += 1
                    self.progress.emit(
                        self._fmt(
                            "log.sync_worker.article_error",
                            title=target_page_title,
                            error_type=type(exc).__name__,
                            error=exc,
                        )
                    )

            if article_count == 0:
                self.progress.emit(self._t("log.sync_worker.articles_none_found"))
            else:
                self.progress.emit(
                    self._fmt(
                        "log.sync_worker.articles_done",
                        success=stats["success"],
                        errors=stats["error"],
                        skipped=stats["skipped"],
                        already=stats["already"],
                    )
                )
        except Exception as exc:
            stats["error"] += 1
            self.progress.emit(
                self._fmt("log.sync_worker.articles_critical_error", category=source_category_name, error=exc)
            )
        return stats

    def _transfer_subcategories(
        self,
        source_category_name: str,
        target_parent_category_name: str,
        source_site: pywikibot.Site,
        target_site: pywikibot.Site,
        *,
        source_members: list[pywikibot.Category],
        row_key: str,
        processed_offset: int = 0,
        category_total: int = 0,
    ) -> dict[str, int]:
        stats = {"success": 0, "error": 0, "skipped": 0, "already": 0, "seen": 0}
        self.progress.emit(
            self._fmt(
                "log.sync_worker.subcategories_processing",
                source=source_category_name,
                target=target_parent_category_name,
            )
        )
        try:
            stats["seen"] = len(source_members)
            subcat_count = len(source_members)
            for idx, source_subcat in enumerate(source_members, start=1):
                if self._stop:
                    break
                self._emit_member_progress(
                    row_key=row_key,
                    target_title=target_parent_category_name,
                    processed=processed_offset + int(idx),
                    total=category_total,
                    mode="subcategories",
                )
                source_subcat_title = source_subcat.title()

                target_subcat_title = self._get_linked_page_title_retry(
                    source_subcat, target_site, expected_ns=14
                )
                if not target_subcat_title:
                    try:
                        target_subcat_title = pywikibot.Category(
                            target_site, source_subcat.title(with_ns=False)
                        ).title()
                    except Exception:
                        target_subcat_title = None

                if not target_subcat_title:
                    stats["skipped"] += 1
                    self.progress.emit(
                        self._fmt("log.sync_worker.subcategory_skip_no_linked", title=source_subcat_title)
                    )
                    continue

                try:
                    target_subcat_page = pywikibot.Category(
                        target_site, target_subcat_title
                    )
                    if not target_subcat_page.exists():
                        stats["skipped"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.subcategory_skip_missing_category", title=target_subcat_page.title())
                        )
                        continue
                    if target_subcat_page.isRedirectPage():
                        stats["skipped"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.subcategory_skip_redirect", title=target_subcat_page.title())
                        )
                        continue
                    if target_subcat_page.namespace().id != 14:
                        stats["skipped"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.subcategory_skip_not_category", title=target_subcat_page.title())
                        )
                        continue

                    text = target_subcat_page.get()
                    if self._category_already_exists(text, target_parent_category_name):
                        stats["already"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.subcategory_already_in_category", title=target_subcat_page.title())
                        )
                        continue

                    new_text = text.rstrip() + f"\n[[{target_parent_category_name}]]\n"
                    if self.edit_summary:
                        summary = self._build_edit_summary(
                            mode="subcategories",
                            source_category_name=source_category_name,
                            target_category_name=target_parent_category_name,
                            source_page_title=source_subcat_title,
                            target_page_title=target_subcat_page.title(),
                        )
                    else:
                        summary = (
                            self._fmt(
                                "log.sync_worker.add_parent_category_summary",
                                target=target_parent_category_name,
                                source_code=source_site.code,
                                source_category=source_category_name,
                            )
                        )
                    if not summary:
                        summary = (
                            self._fmt(
                                "log.sync_worker.add_parent_category_summary",
                                target=target_parent_category_name,
                                source_code=source_site.code,
                                source_category=source_category_name,
                            )
                        )
                    ok = self._save_with_retry(target_subcat_page, new_text, summary, False)
                    if ok:
                        stats["success"] += 1
                        self.progress.emit(
                            self._fmt("log.sync_worker.subcategory_success", title=target_subcat_page.title())
                        )
                    else:
                        stats["error"] += 1
                except Exception as exc:
                    stats["error"] += 1
                    self.progress.emit(
                        self._fmt(
                            "log.sync_worker.subcategory_error",
                            title=target_subcat_title,
                            error_type=type(exc).__name__,
                            error=exc,
                        )
                    )

            if subcat_count == 0:
                self.progress.emit(
                    self._t("log.sync_worker.subcategories_none_found")
                )
            else:
                self.progress.emit(
                    self._fmt(
                        "log.sync_worker.subcategories_done",
                        success=stats["success"],
                        errors=stats["error"],
                        skipped=stats["skipped"],
                        already=stats["already"],
                    )
                )
        except Exception as exc:
            stats["error"] += 1
            self.progress.emit(
                self._fmt("log.sync_worker.subcategories_critical_error", category=source_category_name, error=exc)
            )
        return stats
