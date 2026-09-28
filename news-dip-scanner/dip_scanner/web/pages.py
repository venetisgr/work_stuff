"""Member pages: the dashboard of ranked ideas, an idea, the news and the track record.

A first version from the website's foundation: the dashboard shows the scanner's status and the newest ideas, the
other pages say what they will show. The member pages replace this module and templates/pages/* with the full
versions (router must stay a module-level APIRouter named `router`; create_app includes it).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from . import auth
from .app import render

router = APIRouter()

DASHBOARD_DAYS = 7
DASHBOARD_LIMIT = 30


@router.get("/")
def dashboard(request: Request, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    now = ctx.now()
    stored = ctx.store.opportunities(since=now - timedelta(days=DASHBOARD_DAYS), limit=DASHBOARD_LIMIT)
    ideas = sorted((opp.in_currency(user.settings.currency) for opp in stored), key=lambda o: o.score, reverse=True)
    return render(
        request,
        "pages/dashboard.html",
        {
            "status": ctx.control.status(now=now),
            "ideas": ideas,
            "days": DASHBOARD_DAYS,
            "nav": "ideas",
            "page_title": "Ideas",
        },
    )


@router.get("/ideas/{opportunity_id}")
def idea(request: Request, opportunity_id: int, user: auth.SignedIn, ctx: auth.Ctx) -> Response:
    opp = ctx.store.get_opportunity(opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="There is no idea with that number.")
    return render(
        request,
        "pages/idea.html",
        {"opp": opp.in_currency(user.settings.currency), "nav": "ideas", "page_title": f"{opp.ticker} idea"},
    )


@router.get("/news")
def news(request: Request, user: auth.SignedIn) -> Response:
    return render(request, "pages/placeholder.html", {"nav": "news", "page_title": "News"})


@router.get("/track")
def track(request: Request, user: auth.SignedIn) -> Response:
    return render(request, "pages/placeholder.html", {"nav": "track", "page_title": "Track record"})
