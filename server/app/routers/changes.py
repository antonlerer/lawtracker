from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog as C
from ..changes import build_ctx, get_change, list_changes, overview, serialize, set_action, set_flag
from ..config import Settings
from ..deps import Ctx, current_ctx, get_db, get_settings, need
from ..models import Change, TelegramLink
from ..telegram import effective_status

router = APIRouter(prefix="/api", tags=["changes"])


def today_for(request: Request) -> date:
    clock = getattr(request.app.state, "today", None)
    return clock() if clock else date.today()


def vctx(request: Request, ctx: Ctx, db: Session, settings: Settings):
    return build_ctx(db, ctx.user, ctx.company, settings.enforce_plan_limits, today_for(request))


@router.get("/overview")
def get_overview(request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    tg = effective_status(db.get(TelegramLink, ctx.user.id))
    out = overview(db, v, tg)
    out["greeting_name"] = ctx.user.name.split()[0] if ctx.user.name else ""
    out["company_name"] = (ctx.company.profile or {}).get("name", "") if ctx.company else ""
    return out


@router.get("/changes")
def get_changes(request: Request, q: str = Query("", max_length=200),
                period: Literal["all", "7", "30", "90"] = "all", topic: str = Query("", max_length=10),
                impact: Literal["", "high", "med", "low"] = "",
                status: Literal["all", "new", "read", "saved", "attn"] = "all",
                sort: Literal["date", "impact", "eff"] = "date", personal: bool = False,
                page: int = Query(1, ge=1), per_page: int = Query(5, ge=1, le=50),
                ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    if topic and topic not in C.TOPICS:
        topic = "__none__"  # неизвестная тематика — пустой результат, а не ошибка
    return list_changes(db, v, q=q, period=period, topic=topic, impact=impact, status=status, sort=sort,
                        personal=personal, page=page, per_page=per_page)


@router.get("/changes/{change_id}")
def get_change_detail(change_id: str, request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    ch = get_change(db, v, change_id)
    related = {}
    for rid in ch.related or []:
        r = db.scalar(select(Change).where(Change.id == rid, Change.status == "published"))
        if r:
            related[rid] = r
    return serialize(ch, v, detail=True, related=related)


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool = True


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saved: bool = True


@router.post("/changes/{change_id}/read")
def mark_read(change_id: str, request: Request, body: ReadBody = ReadBody(), ctx: Ctx = Depends(current_ctx),
              db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    st = set_flag(db, v, change_id, read=body.read)
    return {"id": change_id, "is_read": st.read}


@router.post("/changes/{change_id}/save")
def mark_saved(change_id: str, request: Request, body: SaveBody = SaveBody(), ctx: Ctx = Depends(current_ctx),
               db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    st = set_flag(db, v, change_id, saved=body.saved)
    return {"id": change_id, "is_saved": st.saved}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["todo", "done"]


@router.put("/changes/{change_id}/actions/{idx}")
def put_action(change_id: str, idx: int, body: ActionBody, request: Request, ctx: Ctx = Depends(need("member")),
               db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    set_action(db, v, change_id, idx, body.state, ctx.user.id)
    return {"change_id": change_id, "index": idx, "state": body.state}


@router.get("/saved")
def saved(request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)):
    v = vctx(request, ctx, db, settings)
    return list_changes(db, v, status="saved", per_page=50)
