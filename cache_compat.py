"""
cache_compat.py
===============
Lets the same analysis modules run in TWO places:

  1. Inside Streamlit (the web app) — uses st.cache_data, as before.
  2. Inside FastAPI (the phone app's backend) — Streamlit isn't running there,
     so st.cache_data would fail. We fall back to a plain in-memory TTL cache.

Why not just duplicate the code? Because two copies of the Portfolio Doctor's
maths would inevitably drift apart, and one would quietly go wrong. One source
of truth, two runtimes.

USAGE — replace this:
    @st.cache_data(ttl=3600, show_spinner=False)
with:
    @cache_data(ttl=3600)
"""

import functools
import time

# Is Streamlit actually running (i.e. is there a script session)?
def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


def _plain_ttl_cache(ttl: int):
    """A small TTL cache for when Streamlit isn't available."""
    def decorator(fn):
        store: dict = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            hit = store.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
            result = fn(*args, **kwargs)
            store[key] = (now, result)
            # keep the cache from growing forever
            if len(store) > 256:
                oldest = min(store, key=lambda k: store[k][0])
                store.pop(oldest, None)
            return result

        wrapper.clear = store.clear     # mirror st.cache_data's .clear()
        return wrapper
    return decorator


def cache_data(ttl: int = 3600, **_ignored):
    """
    Drop-in replacement for st.cache_data that works in both runtimes.
    Extra kwargs (like show_spinner) are accepted and ignored outside Streamlit.
    """
    def decorator(fn):
        if _in_streamlit():
            import streamlit as st
            return st.cache_data(ttl=ttl, show_spinner=False)(fn)
        return _plain_ttl_cache(ttl)(fn)
    return decorator


def cache_resource(**_ignored):
    """Same idea for st.cache_resource — a plain singleton outside Streamlit."""
    def decorator(fn):
        if _in_streamlit():
            import streamlit as st
            return st.cache_resource(show_spinner=False)(fn)

        cached = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if "v" not in cached:
                cached["v"] = fn(*args, **kwargs)
            return cached["v"]

        return wrapper
    return decorator
