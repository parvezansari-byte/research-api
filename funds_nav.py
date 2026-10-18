"""
funds_nav.py - puts the latest daily NAV (from AMFI) on the Funds screen.

The fund list in funds_data.json is a monthly snapshot (some funds have no NAV
at all). This module replaces the three fund-list routes with thin wrappers
that call the original handlers in api.py and then overlay AMFI's latest NAV,
its date and the 1-day change, matching funds by name. api.py is not edited.
Funds whose name AMFI spells differently keep their snapshot NAV (nav_live
stays false), and /funds-nav/coverage lists which ones didn't match.
"""
import logging

from fastapi import HTTPException

import nav_service

logger = logging.getLogger("funds_nav")
_REPLACED = ("/funds/db/list", "/funds/db/search", "/funds/db/fund")


def overlay(fund: dict) -> bool:
    """Replace a fund dict's NAV fields with AMFI's latest. True if matched."""
    hit = nav_service.lookup_by_name(fund.get("name") or "")
    if not hit:
        return False
    snapshot = fund.get("nav")
    fund["nav"] = hit["nav"]
    fund["nav_live"] = True
    fund["nav_date"] = hit["date"].strftime("%d-%m-%Y")
    fund["nav_day_pct"] = hit["day_pct"]
    fund["nav_change_pct"] = (round((hit["nav"] - snapshot) / snapshot * 100, 2)
                              if snapshot else None)
    return True


def register_funds_nav(app):
    import api  # the module that defines the original handlers

    app.router.routes[:] = [r for r in app.router.routes
                            if getattr(r, "path", None) not in _REPLACED]

    @app.get("/funds/db/list")
    def funds_list(category: str, sort: str = "aum", limit: int = 60):
        result = api.funds_db_list(category, sort, limit)
        nav_service.refresh()
        result["nav_matched"] = sum(1 for f in result["funds"] if overlay(f))
        return result

    @app.get("/funds/db/search")
    def funds_search(q: str):
        result = api.funds_db_search(q)
        nav_service.refresh()
        for f in result["funds"]:
            overlay(f)
        return result

    @app.get("/funds/db/fund")
    def funds_one(name: str):
        result = api.funds_db_fund(name)
        nav_service.refresh()
        overlay(result)
        return result

    @app.get("/funds-nav/coverage")
    def coverage():
        """How many funds in the database match an AMFI scheme by name."""
        nav_service.refresh()
        data = api._load_funds_data()
        missing = [f["name"] for f in data
                   if not nav_service.lookup_by_name(f.get("name") or "")]
        return {"funds": len(data), "matched": len(data) - len(missing),
                "unmatched_sample": missing[:80]}
