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
        subcat_titles: list[str] = []
        subcat_depths: dict[str, int] = {}

        if mode in {"categories_only", "both"}:
            subcat_titles, subcat_depths = self._fetch_subcategory_tree(
                category=category,
                lang=lang,
                fam=fam,
                max_depth=depth,
            )
            categories_only = sorted(
                set(subcat_titles),
                key=lambda value: value.casefold(),
            )
        elif mode == "non_categories_only" and depth > 0:
            subcat_titles, subcat_depths = self._fetch_subcategory_tree(
                category=category,
                lang=lang,
                fam=fam,
                max_depth=depth - 1,
            )

        if mode in {"non_categories_only", "both"} and not self._stop:
            categories_for_pages = [category]
            if depth > 0:
                categories_for_pages.extend(
                    [
                        title
                        for title in subcat_titles
                        if int(subcat_depths.get(title.casefold(), depth + 1)) <= depth
                    ]
                )
            categories_for_pages = list(dict.fromkeys(categories_for_pages))

            page_titles: list[str] = []
            total_categories = len(categories_for_pages)
            for index, current_category in enumerate(categories_for_pages, start=1):
                if self._stop:
                    break
                if index == 1 or index == total_categories or index % 25 == 0:
                    self.progress.emit(
                        self._fmt(
                            "ui.source.fetch_pages_progress",
                            "Reading pages: processed categories {processed}/{total}.",
                            processed=index,
                            total=total_categories,
                        )
                    )
                page_titles.extend(
                    self._fetch_pages_for_category(
                        category=current_category,
                        lang=lang,
                        fam=fam,
                    )
                )
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
            processed += 1
            if processed == 1 or processed % 25 == 0 or not queue:
                self.progress.emit(
                    self._fmt(
                        "ui.source.fetch_subcategories_progress",
                        "Reading subcategories: processed {processed}, found {found}.",
                        processed=processed,
                        found=len(found),
                    )
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

        return found, found_depths
