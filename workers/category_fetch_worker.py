# -*- coding: utf-8 -*-
"""
Фоновый worker для считывания списков страниц и подкатегорий из категории.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QThread, Signal

from ..constants import REQUEST_HEADERS
from ..core.api_client import WikimediaAPIClient
from ..core.localization import translate_runtime


class CategoryFetchWorker(QThread):
    """Считывает содержимое категории в фоне, чтобы не блокировать UI."""

    progress = Signal(str)
    partial_ready = Signal(object, object)
    result_ready = Signal(object, object)
    failed = Signal(str)

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
        categories_snapshot = list(categories or [])
        pages_snapshot = list(pages or [])
        signature = (len(categories_snapshot), len(pages_snapshot))
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
        categories_only: list[str] = []
        non_categories_only: list[str] = []

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

            for member in payload.get("query", {}).get("categorymembers", []):
                title = str(member.get("title", "")).strip()
                if not title:
                    continue
                if int(member.get("ns", 0)) == 14:
                    subcat_titles.append(title)
                else:
                    page_titles.append(title)

            next_continue = payload.get("continue")
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

    def _fetch_direct_subcategories(self, *, category: str, lang: str, fam: str) -> list[str]:
        return self._fetch_category_members(
            category=category,
            lang=lang,
            fam=fam,
            cmtype="subcat",
        )

    def _fetch_category_members(
        self, *, category: str, lang: str, fam: str, cmtype: str
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
            titles.extend([title for title in batch if title])

            next_continue = payload.get("continue")
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
