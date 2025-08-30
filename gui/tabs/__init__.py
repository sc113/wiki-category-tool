# -*- coding: utf-8 -*-
"""
GUI tabs module for Wiki Category Tool.

This module contains all the tab components for the main window:
- AuthTab: Authentication and login
- ParseTab: Reading page content
- ReplaceTab: Replacing page content
- CreateTab: Creating new pages
- RenameTab: Renaming pages and categories
"""

from .auth_tab import AuthTab
from .parse_tab import ParseTab
from .replace_tab import ReplaceTab
from .create_tab import CreateTab
from .rename_tab import RenameTab

__all__ = ['AuthTab', 'ParseTab', 'ReplaceTab', 'CreateTab', 'RenameTab']