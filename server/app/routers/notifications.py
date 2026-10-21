from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog as C
from ..config import Settings
from ..deps import Ctx, current_ctx, get_db, get_settings
from ..errors import ApiError
from ..models import TelegramLink, utcnow
from ..notify import NotifSettings, load_prefs, save_prefs
from ..security import telegram_code
from ..telegram import effective_status, handle_update

router = APIRouter(prefix="/api", tags=["notifications"])


@router.get("/notifications")
def get_notifications(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    return {"settings": load_prefs(db, ctx.user.id).model_dump(mode="json"), "editable": ent["notif_settings"],
            "email_target": ((ctx.company.profile or {}).get("contact_email") if ctx.company else "") or ctx.user.email}


@router.put("/notifications")
def put_notifications(body: NotifSettings, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    if not ent["notif_settings"]:
        raise ApiError(402, "plan_required", "Настройка уведомлений доступна на тарифе «Базовый» и выше")
    save_prefs(db, ctx.user.id, body)
    return {"settings": body.model_dump(mode="json")}


# ---------- Telegram ----------

def _tg_payload(link: TelegramLink | None, settings: Settings) -> dict:
    status = effective_status(link)
    out = {"status": status, "bot": settings.bot_username, "username": link.username if link else "",
           "code": None, "code_expires": None}
    if status == "pending":
        out["code"], out["code_expires"] = link.code, link.code_expires.isoformat()
    return out


@router.get("/telegram")
def telegram_status(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)):
    return _tg_payload(db.get(TelegramLink, ctx.user.id), settings)


@router.post("/telegram/connect")
def telegram_connect(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)):
    ent = C.entitlements(ctx.plan, settings.enforce_plan_limits)
    if not ent["telegram"]:
        raise ApiError(402, "plan_required", "Уведомления в Telegram входят в тариф «Базовый» и выше")
    link = db.get(TelegramLink, ctx.user.id)
    if link is None:
        link = TelegramLink(user_id=ctx.user.id)
        db.add(link)
    while True:  # код должен быть уникальным среди ожидающих
        code = telegram_code()
        if not db.scalar(select(TelegramLink).where(TelegramLink.code == code)):
            break
    link.status, link.code, link.code_expires = "pending", code, utcnow() + timedelta(minutes=15)
    db.commit()
    return _tg_payload(link, settings)


@router.delete("/telegram")
def telegram_disconnect(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db),
                        settings: Settings = Depends(get_settings)):
    link = db.get(TelegramLink, ctx.user.id)
    if link is not None:
        link.status, link.chat_id, link.code, link.code_expires, link.username = "disabled", None, None, None, ""
        db.commit()
    return _tg_payload(link, settings)


@router.post("/telegram/test")
def telegram_test(request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    link = db.get(TelegramLink, ctx.user.id)
    if link is None or effective_status(link) != "connected" or not link.chat_id:
        raise ApiError(409, "telegram_not_connected", "Telegram не подключён")
    ok = request.app.state.telegram.send(link.chat_id, "LawTracker: тестовое сообщение. Всё работает.")
    if not ok:
        raise ApiError(502, "telegram_failed", "Не удалось отправить сообщение")
    return {"ok": True}


@router.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(secret: str, request: Request, db: Session = Depends(get_db),
                           settings: Settings = Depends(get_settings)):
    import hmac
    if not settings.telegram_webhook_secret or not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        raise ApiError(404, "not_found", "Не найдено")
    try:
        update = await request.json()
    except ValueError:
        raise ApiError(400, "bad_request", "Ожидался JSON") from None
    if not isinstance(update, dict):
        raise ApiError(400, "bad_request", "Ожидался объект")
    return {"result": handle_update(db, update, request.app.state.telegram)}
