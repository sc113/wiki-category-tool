"""Category transfer reads and guarded category/Wikidata creation."""

from __future__ import annotations

import pywikibot
from PySide6.QtCore import Signal

from .base_worker import BaseWorker
from .create_worker import _format_summary
from .login_worker import LoginWorker
from ..core.category_transfer import (
    CategoryCreationRow, CategoryItemConflict, CategoryTransferReader, TransferStopped, message,
)
from ..core.pywikibot_config import use_wikidata_profile


class CategoryTransferReadWorker(BaseWorker):
    row_ready = Signal(object)
    read_progress = Signal(object)
    retry_wait = Signal(str)

    def __init__(self, *, source_lang: str, target_lang: str, family: str,
                 delay: float = 0.5, categories: list[dict] | None = None,
                 category: str = "", depth: int = 4, skip_existing_level: int = 2,
                 include_root: bool = False):
        super().__init__("", "", target_lang, family)
        self.reader = CategoryTransferReader(source_lang, target_lang, family,
                                             self._stop_event, delay)
        self.reader.on_retry = self._retry_notice
        self.categories = categories
        self.category = category
        self.depth = depth
        self.skip_existing_level = skip_existing_level
        self.include_root = include_root
        self.results: list[dict] = []
        self.error_count = 0

    def _retry_notice(self, text):
        self.progress.emit(text)
        self.retry_wait.emit(text)

    def run(self):
        try:
            if self.categories is None:
                self.results = self.reader.discover(
                    self.category, self.depth, self.skip_existing_level, self.include_root,
                    self.row_ready.emit,
                    lambda title, count: self.read_progress.emit({"title": title, "count": count}),
                )
            else:
                for row in self.categories:
                    self.reader.check_stop()
                    self.read_progress.emit({"title": row["source_title"], "count": len(self.results)})
                    try:
                        result = self.reader.scan(row)
                    except TransferStopped:
                        raise
                    except Exception as error:
                        self.error_count += 1
                        result = {**row, "status": "error", "error": str(error),
                                  "potential": None, "total": None, "target_articles": []}
                        self.progress.emit(message("row_error", title=row["source_title"], error=error))
                    self.results.append(result)
                    self.row_ready.emit(result)
                    self.item_processed.emit()
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryTransferCreateWorker(BaseWorker):
    category_result = Signal(object)
    interactive_input_requested = Signal(str, bool)

    def __init__(self, *, rows: tuple[CategoryCreationRow, ...], username: str,
                 password: str, source_lang: str, target_lang: str, family: str,
                 summary: str):
        super().__init__(username, password, target_lang, family)
        self.rows = rows
        self.source_lang = source_lang
        self.summary = summary
        self.linked_categories: list[str] = []
        self.stats = {"created": 0, "linked": 0, "labels": 0, "exists": 0, "already": 0, "failed": 0}
        self._login_helper = LoginWorker(username, password, target_lang, family)
        self._login_helper.interactive_input_requested.connect(self.interactive_input_requested)

    def provide_interactive_input(self, answer):
        self._login_helper.provide_interactive_input(answer)

    def request_stop(self):
        super().request_stop()
        self._login_helper.request_stop()

    def _login_sites(self, target_site, repo):
        original = self._login_helper._patch_pywikibot_input(pywikibot)
        try:
            target_site.login(user=self.username)
            if self._stop:
                raise TransferStopped()
            repo.login(user=self.username)
        finally:
            self._login_helper._restore_pywikibot_input(pywikibot, *original)

    @staticmethod
    def _category(site, raw: str):
        # Localized and canonical category prefixes are parsed by Pywikibot.
        page = pywikibot.Category(site, raw)
        if page.namespace().id != 14 or page.section():
            raise ValueError(message("invalid_title", title=raw))
        return page

    @staticmethod
    def _item_for(page):
        if not page.exists():
            return None
        # Refresh pageprops to see sitelinks created during this operation.
        page.properties(force=True)
        if hasattr(page, "_item"):
            del page._item
        try:
            item = pywikibot.ItemPage.fromPage(page)
            item.get(force=True)
            return item
        except pywikibot.exceptions.NoPageError:
            return None

    def _resolve_item(self, source, target):
        source_item = self._item_for(source)
        target_item = self._item_for(target)
        if source_item and target_item and source_item.getID() != target_item.getID():
            raise CategoryItemConflict(source.title(), target.title(), source_item.getID(), target_item.getID())
        item = source_item or target_item
        if item:
            for page in (source, target):
                sitelink = item.sitelinks.get(page.site.dbName())
                if sitelink:
                    # SiteLink.title excludes its namespace. canonical_title()
                    # restores the namespace in the linked wiki's language.
                    linked_title = sitelink.canonical_title()
                    if linked_title.replace("_", " ") != page.title().replace("_", " "):
                        raise ValueError(message("sitelink_conflict", title=page.title(), linked=linked_title))
        return item

    def _create_only(self, page, content: str) -> bool:
        summary = _format_summary(self.summary, content)
        for attempt in range(1, 7):
            if not self._wait_before_save():
                return False
            try:
                page.text = content
                page.save(summary=summary, minor=False, quiet=True, createonly=True)
                self.saved_edits += 1
                self._decay_save_interval()
                return True
            except Exception as error:
                if not self._is_rate_error(error) or attempt == 6:
                    raise
                self._increase_save_interval(attempt)
                wait = max(self._save_min_interval, self._extract_wait_seconds(str(error)))
                self._emit_rate_notice(wait, attempt, 6)
                if not self._interruptible_wait(wait):
                    return False
        return False

    @staticmethod
    def _link_data(source, target, item):
        data = {"sitelinks": {}, "labels": {}}
        for page in (source, target):
            wiki = page.site.dbName()
            if not item or wiki not in item.sitelinks:
                data["sitelinks"][wiki] = {"site": wiki, "title": page.title()}
            code = page.site.lang
            # Repair namespace-free labels generated by earlier versions, while
            # preserving manually chosen labels and other languages.
            label = item.labels.get(code, "") if item else ""
            if not label or label == page.title(with_ns=False):
                data["labels"][code] = page.title()
        return data

    def _link_categories(self, source, target, repo) -> tuple[str, str]:
        # Re-read both sides immediately before writing; never replace a link.
        item = self._resolve_item(source, target)
        data = self._link_data(source, target, item)
        if item is None:
            item = pywikibot.ItemPage(repo)
            claim = pywikibot.Claim(repo, "P31")
            claim.setTarget(pywikibot.ItemPage(repo, "Q4167836"))
            data["claims"] = [claim.toJSON()]
        if not data["sitelinks"] and not data["labels"]:
            return item.getID(), "already_linked"
        if not self._wait_before_save():
            raise TransferStopped()
        # One atomic Wikidata edit includes both sitelinks, labels and new P31.
        # No blind retry of creation: an uncertain response must not duplicate an item.
        summary = ("Link Wikimedia categories and complete category labels" if data["sitelinks"]
                   else "Update Wikimedia category labels")
        item.editEntity(data, summary=summary, bot=True)
        self.saved_edits += 1
        return item.getID(), "linked" if data["sitelinks"] else "labels_updated"

    def run(self):
        try:
            use_wikidata_profile(self.username)
            source_site = pywikibot.Site(self.source_lang, self.family, user=self.username)
            target_site = pywikibot.Site(self.lang, self.family, user=self.username)
            repo = target_site.data_repository()
            if repo is None or source_site.data_repository() != repo:
                raise ValueError(message("no_repository"))
            self._login_sites(target_site, repo)
            # Normalize ALL rows first, before creating any page.
            prepared = []
            sources, targets = set(), set()
            for row in self.rows:
                if self._stop:
                    return
                source = self._category(source_site, row.source_title)
                target = self._category(target_site, row.target_title)
                if source.title() in sources or target.title() in targets:
                    raise ValueError(message("tsv_duplicate", line=len(prepared) + 1))
                sources.add(source.title())
                targets.add(target.title())
                prepared.append((row, source, target))
            for row_index, (row, source, target) in enumerate(prepared, 1):
                if self._stop:
                    break
                created = False
                try:
                    if not source.exists() or source.isRedirectPage():
                        raise ValueError(message("source_missing", title=source.title()))
                    if target.exists() and target.isRedirectPage():
                        raise ValueError(message("target_redirect", title=target.title()))
                    item = self._resolve_item(source, target)
                    data = self._link_data(source, target, item)
                    if target.exists() and item and not data["sitelinks"] and not data["labels"]:
                        qid = item.getID()
                        self.stats["already"] += 1
                        self.linked_categories.append(target.title())
                        self.category_result.emit({"source_title": row.source_title, "target_title": target.title(),
                                                   "qid": qid, "source_qid": qid, "target_qid": qid,
                                                   "status": "already_linked"})
                        self.item_processed.emit()
                        continue
                    if not target.exists():
                        if not row.content.strip():
                            raise ValueError(message("tsv_content_required", line=row_index))
                        if not self._create_only(target, row.content):
                            break
                        created = True
                        self.stats["created"] += 1
                    else:
                        self.stats["exists"] += 1
                    if self._stop:
                        raise TransferStopped()
                    qid, action = self._link_categories(source, target, repo)
                    self.stats[{"linked": "linked", "labels_updated": "labels", "already_linked": "already"}[action]] += 1
                    self.linked_categories.append(target.title())
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(), "qid": qid,
                                               "source_qid": qid, "target_qid": qid,
                                               "status": "created" if created else action})
                    log_key = {"linked": "linked_log", "labels_updated": "labels_updated_log", "already_linked": "skipped_log"}[action]
                    self.progress.emit(message(log_key, title=target.title(), qid=qid))
                except TransferStopped:
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(),
                                               "status": "unlinked" if created else "stopped"})
                    break
                except CategoryItemConflict as error:
                    self.stats["failed"] += 1
                    self.category_result.emit({"source_title": row.source_title, "target_title": target.title(),
                                               "source_qid": error.source_qid, "target_qid": error.target_qid,
                                               "status": "error", "error": str(error)})
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                except Exception as error:
                    self.stats["failed"] += 1
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(),
                                               "status": "unlinked" if created else "error",
                                               "error": str(error)})
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                self.item_processed.emit()
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))


class CategoryTransferPreviewWorker(BaseWorker):
    row_ready = Signal(object)
    retry_wait = Signal(str)

    def __init__(self, *, rows: tuple[CategoryCreationRow, ...], source_lang: str,
                 target_lang: str, family: str, delay: float = 0.5):
        super().__init__("", "", target_lang, family)
        self.rows = rows
        self.results = []
        self.reader = CategoryTransferReader(source_lang, target_lang, family,
                                             self._stop_event, delay)
        self.reader.on_retry = self._retry_notice

    def _retry_notice(self, text):
        self.progress.emit(text)
        self.retry_wait.emit(text)

    def run(self):
        try:
            self.results = self.reader.preview_creation(self.rows, self.row_ready.emit, check_labels=True)
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryTextReadWorker(BaseWorker):
    content_ready = Signal(object)
    retry_wait = Signal(str)

    def __init__(self, *, rows, source_lang, target_lang, family, delay=0.5):
        super().__init__("", "", target_lang, family)
        self.rows = tuple(rows)
        self.reader = CategoryTransferReader(source_lang, target_lang, family, self._stop_event, delay)
        self.reader.on_retry = self.retry_wait.emit
        self.stats = {"loaded": 0, "failed": 0}

    def run(self):
        try:
            normalized = []
            for index, title in self.rows:
                self.reader.check_stop()
                try:
                    normalized.append((index, self.reader.category_title(title, self.reader.source_lang)))
                except ValueError as error:
                    self.stats["failed"] += 1
                    self.progress.emit(message("row_error", title=title, error=error))
                    self.item_processed.emit()
            for offset in range(0, len(normalized), 50):
                batch = normalized[offset:offset + 50]
                pages = self.reader.pages(self.reader.source_lang, [title for _, title in batch], props="info|revisions")
                for index, title in batch:
                    self.reader.check_stop()
                    page = pages.get(title)
                    if not self.reader.valid_page(page, 14):
                        self.stats["failed"] += 1
                        self.progress.emit(message("source_missing", title=title))
                    else:
                        revision = (page.get("revisions") or [{}])[0]
                        slot = revision.get("slots", {}).get("main", revision)
                        if "content" not in slot and "*" not in slot:
                            self.stats["failed"] += 1
                            self.progress.emit(message("content_unavailable", title=title))
                        else:
                            self.content_ready.emit(dict(index=index, content=slot.get("content", slot.get("*", ""))))
                            self.stats["loaded"] += 1
                    self.item_processed.emit()
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryInterwikiPreviewWorker(CategoryTransferPreviewWorker):
    def run(self):
        try:
            self.results = self.reader.preview_interwiki(self.rows, self.row_ready.emit)
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryInterwikiLinkWorker(CategoryTransferCreateWorker):
    """Link only existing pages; leave distinct items for a conflict report."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.conflicts = []
        self.stats = {"linked": 0, "labels": 0, "already": 0, "conflicts": 0, "failed": 0, "items": 0}

    def run(self):
        try:
            use_wikidata_profile(self.username)
            source_site = pywikibot.Site(self.source_lang, self.family, user=self.username)
            target_site = pywikibot.Site(self.lang, self.family, user=self.username)
            repo = target_site.data_repository()
            if repo is None or source_site.data_repository() != repo:
                raise ValueError(message("no_repository"))
            self._login_sites(target_site, repo)
            prepared, sources, targets = [], set(), set()
            for row in self.rows:
                if self._stop:
                    return
                source, target = self._category(source_site, row.source_title), self._category(target_site, row.target_title)
                if source.title() in sources or target.title() in targets:
                    raise ValueError(message("tsv_duplicate", line=len(prepared) + 1))
                sources.add(source.title())
                targets.add(target.title())
                prepared.append((row, source, target))
            for row, source, target in prepared:
                if self._stop:
                    break
                result = dict(source_title=row.source_title, target_title=target.title(), source_qid="", target_qid="")
                try:
                    if not source.exists() or source.isRedirectPage():
                        raise ValueError(message("source_missing", title=source.title()))
                    if not target.exists() or target.isRedirectPage():
                        raise ValueError(message("existing_target_required", title=target.title()))
                    source_item, target_item = self._item_for(source), self._item_for(target)
                    source_qid = source_item.getID() if source_item else ""
                    target_qid = target_item.getID() if target_item else ""
                    result.update(source_qid=source_qid, target_qid=target_qid)
                    if source_qid and target_qid and source_qid != target_qid:
                        result.update(status="different_items")
                        self.conflicts.append(dict(result))
                        self.stats["conflicts"] += 1
                    elif source_qid and source_qid == target_qid:
                        result.update(status="already_linked")
                        self.stats["already"] += 1
                        self.linked_categories.append(target.title())
                    else:
                        qid, action = self._link_categories(source, target, repo)
                        result.update(status=action, source_qid=qid, target_qid=qid)
                        self.stats[{"linked": "linked", "labels_updated": "labels", "already_linked": "already"}[action]] += 1
                        self.stats["items"] += int(not source_qid and not target_qid)
                        self.linked_categories.append(target.title())
                except CategoryItemConflict as error:
                    # A pair may change after preview or during revalidation.
                    result.update(status="different_items", source_qid=error.source_qid, target_qid=error.target_qid)
                    self.conflicts.append(dict(result))
                    self.stats["conflicts"] += 1
                except TransferStopped:
                    break
                except Exception as error:
                    result.update(status="error", error=str(error))
                    self.stats["failed"] += 1
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                self.category_result.emit(result)
                self.item_processed.emit()
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
