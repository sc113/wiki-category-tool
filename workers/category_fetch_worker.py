# -*- coding: utf-8 -*-
"""
Фоновый worker для считывания списков страниц и подкатегорий из категории.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QThread, Signal

from ..constants import REQUEST_HEADERS
from ..core.api_client import WikimediaAPIClient
from ..core.localization import translate_runtime


class CategoryFetchWorker(QThread):
    """Считывает содержимое категории в фоне, чтобы не блокировать UI."""

    progress = Signal(str)
    read_progress = Signal(object)
    partial_ready = Signal(object, object)
    result_ready = Signal(object, object)
    failed = Signal(str)

    PARTIAL_SNAPSHOT_LIMIT = 50000
    MEMBER_PROGRESS_BATCH_INTERVAL = 10
    MEMBER_PROGRESS_SECONDS_INTERVAL = 5.0

    def __init__(self, *, category: str, lang: str, family: str, depth: int, mode: str):
        super().__init__()
        self.category = str(category or "").strip()
        self.lang = str(lang or "ru").strip() or "ru"
        self.family = str(family or "wikipedia").strip() or "wikipedia"
        self.depth = max(0, int(depth or 0))
        self.mode = str(mode or "non_categories_only").strip() or "non_categories_only"
        self._stop = False
        self.api_client = WikimediaAPIClient()
        self._last_partial_signature: tuple[int, int] = (-1, -1)
        self._expected_read_total: int | None = None

    def _t(self, key: str, default: str = "") -> str:
        return translate_runtime(key, default)

    def _fmt(self, key: str, default: str = "", **kwargs) -> str:
        text = self._t(key, default)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def request_stop(self) -> None:
        self._stop = True

    def _emit_partial_snapshot(self, *, categories: list[str], pages: list[str]) -> None:
        if self._stop:
            return
        categories_count = len(categories or [])
        pages_count = len(pages or [])
        if categories_count + pages_count > self.PARTIAL_SNAPSHOT_LIMIT:
            return

        categories_snapshot = list(categories or [])
        pages_snapshot = list(pages or [])
        signature = (categories_count, pages_count)
        if signature == self._last_partial_signature:
            return
        self._last_partial_signature = signature

        combined_titles = list(categories_snapshot)
        existing_keys = {value.casefold() for value in combined_titles}
        for title in pages_snapshot:
            title_key = title.casefold()
            if title_key in existing_keys:
                continue
            combined_titles.append(title)
            existing_keys.add(title_key)

        self.partial_ready.emit(
            combined_titles,
            {
                "categories": len(categories_snapshot),
                "non_categories": len(pages_snapshot),
            },
        )

    def _emit_read_progress(
        self,
        *,
        category: str,
        depth: int,
        batch: int,
        batch_pages: int,
        batch_subcategories: int,
        pages: int,
        subcategories: int,
        processed: int,
        queued: int,
        total: int | None = None,
    ) -> None:
        if self._stop:
            return
        expected_total = total
        if expected_total is None:
            expected_total = self._expected_read_total

        payload = {
            "category": category,
            "depth": int(depth or 0),
            "batch": int(batch or 0),
            "batch_pages": int(batch_pages or 0),
            "batch_subcategories": int(batch_subcategories or 0),
            "pages": int(pages or 0),
            "subcategories": int(subcategories or 0),
            "processed": int(processed or 0),
            "queued": int(queued or 0),
            "count": int(pages or 0) + int(subcategories or 0),
        }
        if expected_total is not None and int(expected_total or 0) > 0:
            payload["total"] = int(expected_total or 0)
        self.read_progress.emit(payload)
        self.progress.emit(
            self._fmt(
                "ui.source.fetch_members_batch_progress",
                (
                    "Reading members: {category} (depth {depth}, batch {batch}) - "
                    "+{batch_pages} pages, +{batch_subcategories} subcategories; "
                    "totals: pages {pages}, subcategories {subcategories}; "
                    "processed categories {processed}, queued {queued}."
                ),
                **payload,
            )
        )

    def _fetch_category_info_counts(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
    ) -> dict[str, int]:
        api_url = self.api_client._build_api_url(fam, lang)
        params = {
            "action": "query",
            "prop": "categoryinfo",
            "titles": category,
            "format": "json",
        }
        retries = 0
        while not self._stop:
            try:
                self.api_client._rate_wait()
                response = self.api_client.session.get(
                    api_url,
                    params=params,
                    timeout=10,
                    headers=REQUEST_HEADERS,
                )
            except Exception:
                if retries < 2:
                    retries += 1
                    self.api_client._rate_backoff(0.6 * retries)
                    continue
                return {}

            if response.status_code == 429 and retries < 3:
                retries += 1
                self.api_client._rate_backoff(0.8 * retries)
                continue
            if response.status_code != 200:
                return {}

            try:
                pages = response.json().get("query", {}).get("pages", {})
            except Exception:
                return {}

            for page in pages.values():
                info = page.get("categoryinfo") or {}
                if not isinstance(info, dict):
                    continue
                result: dict[str, int] = {}
                for key in ("pages", "subcats", "files", "size"):
                    try:
                        result[key] = max(0, int(info.get(key, 0) or 0))
                    except Exception:
                        result[key] = 0
                return result
            return {}
        return {}

    def _announce_expected_read_total(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        include_pages: bool,
        include_subcats: bool,
    ) -> None:
        counts = self._fetch_category_info_counts(
            category=category,
            lang=lang,
            fam=fam,
        )
        if not counts or self._stop:
            return

        pages = counts.get("pages", 0) if include_pages else 0
        subcategories = counts.get("subcats", 0) if include_subcats else 0
        total = int(pages or 0) + int(subcategories or 0)
        if total <= 0:
            return

        self._expected_read_total = total
        self.read_progress.emit(
            {
                "category": category,
                "depth": 0,
                "batch": 0,
                "batch_pages": 0,
                "batch_subcategories": 0,
                "pages": 0,
                "subcategories": 0,
                "processed": 0,
                "queued": 0,
                "count": 0,
                "total": total,
            }
        )
        self.progress.emit(
            self._fmt(
                "ui.source.fetch_members_total_known",
                (
                    "Category total to read: {total} "
                    "(pages {pages}, subcategories {subcategories})."
                ),
                total=total,
                pages=pages,
                subcategories=subcategories,
                category=category,
            )
        )

    def run(self) -> None:
        try:
            titles, stats = self._fetch_titles_for_mode(
                category=self.category,
                lang=self.lang,
                fam=self.family,
                depth=self.depth,
                mode=self.mode,
            )
            if self._stop:
                return
            self.result_ready.emit(titles, stats)
        except Exception as exc:
            self.failed.emit(
                self._fmt("ui.source.api_error", "API error: {error}", error=exc)
            )

    def _fetch_titles_for_mode(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        depth: int,
        mode: str,
    ) -> tuple[list[str], dict[str, int]]:
        self._expected_read_total = None
        categories_only: list[str] = []
        non_categories_only: list[str] = []

        if depth == 0:
            self._announce_expected_read_total(
                category=category,
                lang=lang,
                fam=fam,
                include_pages=mode in {"non_categories_only", "both"},
                include_subcats=mode in {"categories_only", "both"},
            )

        if mode == "categories_only":
            subcat_titles, _subcat_depths = self._fetch_subcategory_tree(
                category=category,
                lang=lang,
                fam=fam,
                max_depth=depth,
            )
            categories_only = sorted(
                set(subcat_titles),
                key=lambda value: value.casefold(),
            )
        else:
            page_depth = depth if mode in {"non_categories_only", "both"} else -1
            subcategory_depth = depth + 1 if mode == "both" else 0

            page_titles, subcat_titles, _subcat_depths = self._walk_category_tree_combined(
                category=category,
                lang=lang,
                fam=fam,
                page_depth=page_depth,
                subcategory_depth=subcategory_depth,
            )

            if mode == "both":
                categories_only = sorted(
                    set(subcat_titles),
                    key=lambda value: value.casefold(),
                )
            if mode in {"non_categories_only", "both"}:
                non_categories_only = sorted(
                    set(page_titles),
                    key=lambda value: value.casefold(),
                )

        combined_titles = list(categories_only)
        existing_keys = {value.casefold() for value in combined_titles}
        for title in non_categories_only:
            title_key = title.casefold()
            if title_key in existing_keys:
                continue
            combined_titles.append(title)
            existing_keys.add(title_key)

        return combined_titles, {
            "categories": len(categories_only),
            "non_categories": len(non_categories_only),
        }

    def _walk_category_tree_combined(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        page_depth: int,
        subcategory_depth: int,
    ) -> tuple[list[str], list[str], dict[str, int]]:
        if self._stop or (page_depth < 0 and subcategory_depth <= 0):
            return [], [], {}

        visited: set[str] = {category.casefold()}
        queue = deque([(category, 0)])
        page_titles: list[str] = []
        page_seen: set[str] = set()
        found_subcats: list[str] = []
        found_subcat_depths: dict[str, int] = {}
        processed = 0

        while queue and not self._stop:
            current_category, current_depth = queue.popleft()
            need_pages = page_depth >= 0 and current_depth <= page_depth
            need_subcats = current_depth < max(page_depth, subcategory_depth)
            current_pages, current_subcats = self._fetch_category_members_split(
                category=current_category,
                lang=lang,
                fam=fam,
                include_pages=need_pages,
                include_subcats=need_subcats,
                progress_base_pages=len(page_titles),
                progress_base_subcategories=len(found_subcats),
                processed_categories=processed,
                queued_categories=len(queue),
                current_depth=current_depth,
            )

            if current_pages:
                for title in current_pages:
                    title_key = title.casefold()
                    if title_key in page_seen:
                        continue
                    page_seen.add(title_key)
                    page_titles.append(title)

            child_depth = current_depth + 1
            for subcat in current_subcats:
                subcat_key = subcat.casefold()
                if subcat_key in visited:
                    continue
                visited.add(subcat_key)

                if child_depth <= subcategory_depth:
                    found_subcats.append(subcat)
                    found_subcat_depths[subcat_key] = child_depth

                if child_depth <= page_depth or child_depth < subcategory_depth:
                    queue.append((subcat, child_depth))

            processed += 1
            if processed == 1 or processed % 10 == 0 or not queue:
                self._emit_partial_snapshot(
                    categories=found_subcats,
                    pages=page_titles,
                )
            if processed == 1 or processed % 25 == 0 or not queue:
                self.progress.emit(
                    self._fmt(
                        "ui.source.fetch_tree_progress",
                        "Reading category tree: processed {processed}, subcategories {subcategories}, pages {pages}.",
                        processed=processed,
                        subcategories=len(found_subcats),
                        pages=len(page_titles),
                    )
                )

        return page_titles, found_subcats, found_subcat_depths

    def _fetch_category_members_split(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        include_pages: bool,
        include_subcats: bool,
        progress_base_pages: int = 0,
        progress_base_subcategories: int = 0,
        processed_categories: int = 0,
        queued_categories: int = 0,
        current_depth: int = 0,
    ) -> tuple[list[str], list[str]]:
        cmtypes: list[str] = []
        if include_pages:
            cmtypes.append("page")
        if include_subcats:
            cmtypes.append("subcat")
        if not cmtypes:
            return [], []

        api_url = self.api_client._build_api_url(fam, lang)
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmtype": "|".join(cmtypes),
            "cmlimit": "max",
            "format": "json",
        }

        page_titles: list[str] = []
        subcat_titles: list[str] = []
        retries = 0
        batches = 0
        last_progress_at = 0.0
        while not self._stop:
            try:
                self.api_client._rate_wait()
                response = self.api_client.session.get(
                    api_url,
                    params=params,
                    timeout=10,
                    headers=REQUEST_HEADERS,
                )
            except Exception as exc:
                if retries < 2:
                    retries += 1
                    self.api_client._rate_backoff(0.6 * retries)
                    continue
                self.progress.emit(
                    self._fmt(
                        "ui.source.api_error",
                        "API error: {error}",
                        error=exc,
                    )
                )
                break

            if response.status_code == 429 and retries < 3:
                retries += 1
                self.api_client._rate_backoff(0.8 * retries)
                continue

            if response.status_code != 200:
                self.progress.emit(
                    self._fmt(
                        "ui.source.http_error_tree",
                        "HTTP {status} while fetching category members for {category}",
                        status=response.status_code,
                        category=category,
                    )
                )
                break

            retries = 0
            try:
                payload = response.json()
            except Exception:
                self.progress.emit(
                    self._fmt(
                        "ui.source.json_parse_error",
                        "Failed to parse JSON for {category}",
                        category=category,
                    )
                )
                break

            batch_pages = 0
            batch_subcategories = 0
            for member in payload.get("query", {}).get("categorymembers", []):
                title = str(member.get("title", "")).strip()
                if not title:
                    continue
                if int(member.get("ns", 0)) == 14:
                    subcat_titles.append(title)
                    batch_subcategories += 1
                else:
                    page_titles.append(title)
                    batch_pages += 1

            next_continue = payload.get("continue")
            batches += 1
            now = time.monotonic()
            should_emit_progress = (
                batches == 1
                or batches % self.MEMBER_PROGRESS_BATCH_INTERVAL == 0
                or not next_continue
                or (now - last_progress_at) >= self.MEMBER_PROGRESS_SECONDS_INTERVAL
            )
            if should_emit_progress:
                last_progress_at = now
                self._emit_read_progress(
                    category=category,
                    depth=current_depth,
                    batch=batches,
                    batch_pages=batch_pages,
                    batch_subcategories=batch_subcategories,
                    pages=int(progress_base_pages or 0) + len(page_titles),
                    subcategories=(
                        int(progress_base_subcategories or 0) + len(subcat_titles)
                    ),
                    processed=processed_categories,
                    queued=queued_categories,
                )
            if not next_continue:
                break
            params.update(next_continue)

        return page_titles, subcat_titles

    def _fetch_pages_for_category(self, *, category: str, lang: str, fam: str) -> list[str]:
        return self._fetch_category_members(
            category=category,
            lang=lang,
            fam=fam,
            cmtype="page",
        )

    def _fetch_direct_subcategories(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        progress_base_subcategories: int = 0,
        processed_categories: int = 0,
        queued_categories: int = 0,
        current_depth: int = 0,
    ) -> list[str]:
        return self._fetch_category_members(
            category=category,
            lang=lang,
            fam=fam,
            cmtype="subcat",
            progress_base_subcategories=progress_base_subcategories,
            processed_categories=processed_categories,
            queued_categories=queued_categories,
            current_depth=current_depth,
        )

    def _fetch_category_members(
        self,
        *,
        category: str,
        lang: str,
        fam: str,
        cmtype: str,
        progress_base_pages: int = 0,
        progress_base_subcategories: int = 0,
        processed_categories: int = 0,
        queued_categories: int = 0,
        current_depth: int = 0,
    ) -> list[str]:
        api_url = self.api_client._build_api_url(fam, lang)
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmtype": cmtype,
            "cmlimit": "max",
            "format": "json",
        }

        titles: list[str] = []
        retries = 0
        batches = 0
        last_progress_at = 0.0
        while not self._stop:
            try:
                self.api_client._rate_wait()
                response = self.api_client.session.get(
                    api_url,
                    params=params,
                    timeout=10,
                    headers=REQUEST_HEADERS,
                )
            except Exception as exc:
                if retries < 2:
                    retries += 1
                    self.api_client._rate_backoff(0.6 * retries)
                    continue
                self.progress.emit(
                    self._fmt(
                        "ui.source.api_error",
                        "API error: {error}",
                        error=exc,
                    )
                )
                break

            if response.status_code == 429 and retries < 3:
                retries += 1
                self.api_client._rate_backoff(0.8 * retries)
                continue

            if response.status_code != 200:
                key = (
                    "ui.source.http_error_subcategories"
                    if cmtype == "subcat"
                    else "ui.source.http_error_pages"
                )
                fallback = (
                    "HTTP {status} while fetching subcategories for {category}"
                    if cmtype == "subcat"
                    else "HTTP {status} while fetching pages for {category}"
                )
                self.progress.emit(
                    self._fmt(
                        key,
                        fallback,
                        status=response.status_code,
                        category=category,
                    )
                )
                break

            retries = 0
            try:
                payload = response.json()
            except Exception:
                self.progress.emit(
                    self._fmt(
                        "ui.source.json_parse_error",
                        "Failed to parse JSON for {category}",
                        category=category,
                    )
                )
                break

            batch = [
                str(member.get("title", "")).strip()
                for member in payload.get("query", {}).get("categorymembers", [])
            ]
            batch_titles = [title for title in batch if title]
            titles.extend(batch_titles)

            next_continue = payload.get("continue")
            batches += 1
            now = time.monotonic()
            should_emit_progress = (
                batches == 1
                or batches % self.MEMBER_PROGRESS_BATCH_INTERVAL == 0
                or not next_continue
                or (now - last_progress_at) >= self.MEMBER_PROGRESS_SECONDS_INTERVAL
            )
            if should_emit_progress:
                last_progress_at = now
                pages_total = int(progress_base_pages or 0)
                subcategories_total = int(progress_base_subcategories or 0)
                if cmtype == "subcat":
                    subcategories_total += len(titles)
                    batch_pages = 0
                    batch_subcategories = len(batch_titles)
                else:
                    pages_total += len(titles)
                    batch_pages = len(batch_titles)
                    batch_subcategories = 0
                self._emit_read_progress(
                    category=category,
                    depth=current_depth,
                    batch=batches,
                    batch_pages=batch_pages,
                    batch_subcategories=batch_subcategories,
                    pages=pages_total,
                    subcategories=subcategories_total,
                    processed=processed_categories,
                    queued=queued_categories,
                )
            if not next_continue:
                break
            params.update(next_continue)

        return titles

    def _fetch_subcategory_tree(
        self, *, category: str, lang: str, fam: str, max_depth: int
    ) -> tuple[list[str], dict[str, int]]:
        if max_depth < 0 or self._stop:
            return [], {}

        visited: set[str] = {category.casefold()}
        queue = deque([(category, 0)])
        found: list[str] = []
        found_depths: dict[str, int] = {}
        processed = 0

        while queue and not self._stop:
            current_category, current_depth = queue.popleft()
            direct_subcats = self._fetch_direct_subcategories(
                category=current_category,
                lang=lang,
                fam=fam,
                progress_base_subcategories=len(found),
                processed_categories=processed,
                queued_categories=len(queue),
                current_depth=current_depth,
            )
            child_depth = current_depth + 1
            for subcat in direct_subcats:
                subcat_key = subcat.casefold()
                if subcat_key in visited:
                    continue
                visited.add(subcat_key)
                found.append(subcat)
                found_depths[subcat_key] = child_depth
                if current_depth < max_depth:
                    queue.append((subcat, child_depth))

            processed += 1
            if processed == 1 or processed % 10 == 0 or not queue:
                self._emit_partial_snapshot(categories=found, pages=[])
            if processed == 1 or processed % 25 == 0 or not queue:
                self.progress.emit(
                    self._fmt(
                        "ui.source.fetch_subcategories_progress",
                        "Reading subcategories: processed {processed}, found {found}.",
                        processed=processed,
                        found=len(found),
                    )
                )

        return found, found_depths
