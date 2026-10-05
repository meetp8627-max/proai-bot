"""ProAI Telegram Mini App backend: auth + AI chat + Xiaomi tools API."""

import re
import hmac
import json
import time
import hashlib
import asyncio
import logging
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import parse_qsl

from bs4 import BeautifulSoup
from starlette.routing import Route
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse

import xiaomi as X

log = logging.getLogger("proai.miniapp")
INDEX = Path(__file__).parent / "webapp.html"


# ───────────────────────── Telegram initData auth ─────────────────────────
def verify_init_data(init_data: str, token: str, max_age: int = 86400):
    """Telegram Mini App initData verify karo. Valid ho to user dict, warna None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got = parsed.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        return json.loads(parsed["user"]) if "user" in parsed else None
    except Exception:
        return None


# ───────────────────────── Xiaomi aggregators ─────────────────────────
async def search_devices(q: str):
    q = q.strip().lower()
    if not q:
        return []
    nm = await X.names()
    out = []
    if q in nm:
        out.append({"codename": q, "name": nm[q]})
    for cn, name in nm.items():
        if cn == q:
            continue
        parts = [p.strip().lower() for p in str(name).split("/")]
        if q in cn or any(q in p for p in parts):
            out.append({"codename": cn, "name": name})
    return out[:30]


def _pick(rows):
    out = []
    for r in X.pick_roms(rows, "Recovery"):
        out.append({**r, "method": "Recovery"})
    return out


async def t_roms(cn):
    rows = (await X.miui_roms()).get(cn)
    if not rows:
        return None
    rec = X.pick_roms(rows, "Recovery")[:30]
    fb = X.pick_roms(rows, "Fastboot")[:30]
    return {"recovery": rec, "fastboot": fb}


async def t_specs(cn):
    hits = [i for i in await X.specs_data() if cn in i["codenames"]]
    if not hits:
        return None
    i = hits[0]
    s = i["specs"]
    g = lambda sec, key: (s.get(sec) or {}).get(key)
    cam = lambda sec: " ".join(next(iter((s.get(sec) or {"": ""}).items())))
    return {"name": i["name"], "url": i["url"], "picture": i.get("picture"),
            "rows": [r for r in [
                ("Status", g("Launch", "Status")), ("Network", g("Network", "Technology")),
                ("Weight", g("Body", "Weight")),
                ("Display", ", ".join(x for x in [g("Display", "Type"), g("Display", "Size"), g("Display", "Resolution")] if x)),
                ("Chipset", g("Platform", "Chipset")), ("CPU", g("Platform", "CPU")), ("GPU", g("Platform", "GPU")),
                ("Memory", g("Memory", "Internal")), ("Rear camera", cam("Main Camera").strip()),
                ("Front camera", cam("Selfie camera").strip()), ("USB", g("Comms", "USB")),
                ("3.5mm jack", g("Sound", "3.5mm jack")), ("Sensors", g("Features", "Sensors")),
                ("Battery", g("Battery", "Type")), ("Charging", g("Battery", "Charging"))] if r[1]],
            "others": [x["name"] for x in hits[1:3]]}


async def t_models(cn):
    d = (await X.models()).get(cn)
    if not d:
        return None
    return {"name": d["name"], "internal": d["internal_name"],
            "models": [{"model": k.strip("`"), "name": v} for k, v in d["models"].items()]}


async def t_twrp(cn):
    devs = await X.twrp_devices()
    if cn not in devs:
        return None
    link = devs[cn]["link"]
    row = BeautifulSoup(await X.fetch(link), "html.parser").find("table").find("tr")
    a = row.find("a")
    return {"name": devs[cn]["name"], "file": a.text, "link": f"https://dl.twrp.me{a['href']}",
            "size": row.find("span", {"class": "filesize"}).text, "date": row.find("em").text.strip(), "page": link}


async def t_pb(cn):
    links = [i for i in await X.pb_links() if cn in i]
    if not links:
        return None
    return {"file": links[0].split("/")[-2], "link": links[0]}


async def t_of(cn):
    api = "https://api.orangefox.download/v3"
    d = await X.load_json(f"{api}/devices/get?codename={cn}")
    out = {"name": d.get("full_name", cn), "maintainer": (d.get("maintainer") or {}).get("name"),
           "page": f"https://orangefox.download/device/{cn}", "builds": []}
    for typ in ("stable", "beta"):
        rl = await X.load_json(f"{api}/releases/?device_id={d['_id']}&type={typ}&limit=1")
        if rl.get("data"):
            rel = await X.load_json(f"{api}/releases/get?_id={rl['data'][0]['_id']}")
            out["builds"].append({"type": typ, "file": rel["filename"], "url": rel["url"]})
    return out


async def t_eu(cn):
    devs = await X.eu_codenames()
    if cn not in devs:
        return None
    code = re.escape(devs[cn][1])
    links = await X.eu_links()
    weekly = [i for i in links if re.search(rf"{code}_(?:V|OS).*DEV", i)]
    stable = [i for i in links if re.search(rf"{code}_(?:V|OS)", i) and i not in weekly]
    f = lambda l: {"file": l.split("/")[-2], "link": l} if l else None
    return {"name": devs[cn][0], "stable": f(stable[0] if stable else None),
            "weekly": f(weekly[0] if weekly else None)}


async def t_fw(cn):
    rows = (await X.firmware_data()).get(cn)
    return rows[:8] if rows else None


async def device_detail(cn):
    names = await X.names()
    keys = ["specs", "models", "roms", "twrp", "pb", "of", "eu", "firmware"]
    res = await asyncio.gather(t_specs(cn), t_models(cn), t_roms(cn), t_twrp(cn), t_pb(cn),
                               t_of(cn), t_eu(cn), t_fw(cn), return_exceptions=True)
    out = {"codename": cn, "name": names.get(cn, cn)}
    for k, v in zip(keys, res):
        if isinstance(v, Exception):
            log.warning("detail %s/%s failed: %r", cn, k, v)
            out[k] = None
        else:
            out[k] = v
    out["archive"] = {"miui": f"{X.SITE}/archive/miui/{cn}/", "hyperos": f"{X.SITE}/archive/hyperos/{cn}/"}
    return out


# ───────────────────────── Routes ─────────────────────────
def build_routes(token, ask_ai, history):
    hits = defaultdict(lambda: deque(maxlen=20))

    def auth(request):
        user = verify_init_data(request.headers.get("X-Init-Data", ""), token)
        if not user:
            return None, JSONResponse({"error": "Telegram ke andar se kholo (login verify nahi hua)"}, status_code=401)
        return user, None

    def limited(uid, limit=15):
        now = time.time()
        q = hits[uid]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False

    async def index(request):
        return HTMLResponse(INDEX.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})

    async def health(request):
        return PlainTextResponse("ok")

    async def chat(request):
        user, err = auth(request)
        if err:
            return err
        if limited(user["id"]):
            return JSONResponse({"error": "Thoda dheere bhai 😅 1 minute baad try karo"}, status_code=429)
        body = await request.json()
        text = str(body.get("text", "")).strip()[:4000]
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        reply = await ask_ai(f"web:{user['id']}", f"{user.get('first_name', 'User')}: {text}")
        return JSONResponse({"reply": reply})

    async def reset(request):
        user, err = auth(request)
        if err:
            return err
        history[f"web:{user['id']}"].clear()
        return JSONResponse({"ok": True})

    async def search(request):
        user, err = auth(request)
        if err:
            return err
        try:
            return JSONResponse({"results": await search_devices(request.query_params.get("q", ""))})
        except Exception as e:
            log.exception("search")
            return JSONResponse({"error": f"Data load nahi hua ({type(e).__name__})"}, status_code=502)

    async def device(request):
        user, err = auth(request)
        if err:
            return err
        cn = request.query_params.get("cn", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,40}", cn) or cn not in await X.names():
            return JSONResponse({"error": "Device nahi mila"}, status_code=404)
        try:
            return JSONResponse(await device_detail(cn))
        except Exception as e:
            log.exception("device")
            return JSONResponse({"error": f"Data load nahi hua ({type(e).__name__})"}, status_code=502)

    return [
        Route("/", index), Route("/health", health),
        Route("/api/chat", chat, methods=["POST"]), Route("/api/reset", reset, methods=["POST"]),
        Route("/api/xiaomi/search", search), Route("/api/xiaomi/device", device),
    ]
