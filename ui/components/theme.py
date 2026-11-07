"""Light/dark theme switch.

Streamlit decides the theme in the browser: it follows the computer's light/dark setting unless the viewer
picked one, which the browser remembers (localStorage key "stActiveTheme-<page path>-v2"). A server-side
setting can't change it, so the switch writes that browser setting for every page and reloads the page."""
from __future__ import annotations

import json

import streamlit as st

from ui.components.nav import PAGES

CHOICES = {"light": ":material/light_mode: Light", "dark": ":material/dark_mode: Dark"}


def current() -> str:
    """The theme the browser is showing ('light' or 'dark')."""
    return getattr(st.context.theme, "type", None) or "light"


def _apply(name: str) -> None:
    paths = sorted({"/"} | {f"/{p.url_path}" for p in PAGES.values() if p.url_path})
    st.html(f"""<script>
      (() => {{
        const value = {json.dumps(json.dumps(name.title()))};
        for (const path of {json.dumps(paths)}.concat([window.location.pathname]))
          try {{ window.localStorage.setItem(`stActiveTheme-${{path}}-v2`, value); }} catch (e) {{}}
        window.location.reload();
      }})();
    </script>""", unsafe_allow_javascript=True)


def switcher(where=st) -> None:
    now = current()
    picked = where.segmented_control("Theme", list(CHOICES), format_func=CHOICES.get, default=now,
                                     key=f"theme-{now}")    # re-created when the theme changes
    if picked and picked != now:
        _apply(picked)
