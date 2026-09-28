"""The website (`dip-scanner serve`): server-rendered pages on FastAPI, for the owner and a few invited people.

Needs the web extra: pip install -e ".[web]". Templates and static files live in templates/ and static/ next to this
file and ship with the package.

Modules: app.py (create_app, render/redirect/flash, templates and filters, headers, error pages), context.py (the
AppContext of services), auth.py (sessions, CSRF, Origin checks, the dependencies), account.py (sign-in, invite,
password and settings pages), jobs.py ("Analyse now"), control.py (the scanner's loop in a thread), server.py
(`serve`), pages.py and admin.py (member and admin pages).

A page is a `def` handler (it may read the database) on a module-level APIRouter:

    from fastapi import APIRouter, Request
    from . import auth
    from .app import render, redirect

    router = APIRouter()

    @router.get("/news")
    def news(request: Request, user: auth.SignedIn, ctx: auth.Ctx):
        return render(request, "pages/news.html", {"nav": "news", "page_title": "News"})

    @router.post("/tickers/{symbol}/watch")
    def watch(request: Request, symbol: str, user: auth.SignedIn, form: auth.SignedForm, ctx: auth.Ctx):
        ...
        return redirect(request, f"/tickers/{symbol}", "Added to your watchlist.")

Templates extend base.html, import the macros with `{% import "_macros.html" as ui with context %}`, put
`{{ ui.csrf_field() }}` in every POST form, and use no inline scripts, styles or event handlers (the CSP blocks
them): classes from static/app.css and data-attributes for static/app.js.
"""
