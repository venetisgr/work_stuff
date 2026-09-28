"""Admin pages: the scanner (status, pause, resume, a cycle now), users and invites.

A first version from the website's foundation: /admin shows the scanner's status. The admin pages replace this
module and templates/admin/* with the full versions (router must stay a module-level APIRouter named `router`, with
prefix /admin; create_app includes it). Every route here depends on auth.require_admin (auth.Admin).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import auth
from .app import render

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, user: auth.Admin, ctx: auth.Ctx) -> Response:
    return render(
        request,
        "admin/index.html",
        {"status": ctx.control.status(now=ctx.now()), "nav": "admin", "page_title": "Admin"},
    )
