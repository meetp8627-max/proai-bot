"""ProAI Telegram Mini App backend (Starlette).
Serves index.html, secure JSON API (Telegram initData verified) and the bot webhook."""

import os
import time
import hmac
import json
import hashlib
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.responses import JSONResponse, FileResponse, PlainTextResponse
from starlette.routing import Route
from telegram import Update, MenuButtonWebApp, WebAppInfo

import xiaomi

log = logging.getLogger("proai.web")
HERE = os.path.dirname(os.path.abspath(__file__))


def validate_init_data(init_data: str, token: str, max_age: int = 86400):
    """Telegram WebApp initData verify karo. Valid ho to user dict, warna None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def create_app(token, application, ai_reply, reset_history, base_url, post_init=None):
    secret = hashlib.sha256(("wh:" + token).encode()).hexdigest()[:32]
    hits = defaultdict(lambda: deque(maxlen=30))

    def auth(request):
        return validate_init_data(request.headers.get("x-init-data", ""), token)

    def limited(uid, limit=15, window=60):
        now, q = time.time(), hits[uid]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False

    def guard(fn):
        async def wrapper(request):
            user = auth(request)
            if not user:
                return JSONResponse({"error": "Telegram ke andar se kholo (login verify nahi hua)"}, status_code=401)
            request.state.user = user
            try:
                return await fn(request)
            except Exception as e:
                log.exception("api error")
                return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
        return wrapper

    async def index(request):
        return FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})

    async def health(request):
        return PlainTextResponse("ok")

    async def telegram_webhook(request):
        if request.headers.get("x-telegram-bot-api-secret-token") != secret:
            return PlainTextResponse("forbidden", status_code=403)
        update = Update.de_json(await request.json(), application.bot)
        await application.update_queue.put(update)
        return PlainTextResponse("ok")

    @guard
    async def me(request):
        u = request.state.user
        return JSONResponse({"id": u.get("id"), "first_name": u.get("first_name", "")})

    @guard
    async def chat(request):
        u = request.state.user
        if limited(u["id"]):
            return JSONResponse({"error": "Thoda dheere bhai 😅 1 minute baad try karo"}, status_code=429)
        body = await request.json()
        text = (body.get("message") or "").strip()[:4000]
        if not text:
            return JSONResponse({"error": "Empty message"}, status_code=400)
        return JSONResponse({"reply": await ai_reply(u["id"], text)})

    @guard
    async def reset(request):
        reset_history(request.state.user["id"])
        return JSONResponse({"ok": True})

    @guard
    async def search(request):
        return JSONResponse({"results": await xiaomi.search_devices(request.query_params.get("q", ""))})

    @guard
    async def device(request):
        d = await xiaomi.device_detail(request.path_params["cn"].lower())
        if not d:
            return JSONResponse({"error": "Device nahi mila"}, status_code=404)
        return JSONResponse(d)

    @guard
    async def recoveries(request):
        return JSONResponse(await xiaomi.recoveries(request.path_params["cn"].lower()))

    @asynccontextmanager
    async def lifespan(_):
        async with application:
            await application.start()
            if post_init:
                await post_init(application)
            await application.bot.set_webhook(url=f"{base_url}/telegram", secret_token=secret,
                                              allowed_updates=Update.ALL_TYPES)
            try:
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="ProAI", web_app=WebAppInfo(url=base_url)))
            except Exception:
                log.exception("menu button set nahi hua")
            log.info("Webhook + Mini App ready: %s", base_url)
            yield
            await application.stop()

    routes = [
        Route("/", index), Route("/health", health),
        Route("/telegram", telegram_webhook, methods=["POST"]),
        Route("/api/me", me), Route("/api/chat", chat, methods=["POST"]), Route("/api/reset", reset, methods=["POST"]),
        Route("/api/xiaomi/search", search), Route("/api/xiaomi/device/{cn}", device),
        Route("/api/xiaomi/recoveries/{cn}", recoveries),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
