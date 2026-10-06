"""Xiaomi Geeks (Uranus) style commands for ProAI.
Data sources (same as the original Uranus bot): XiaomiFirmwareUpdater GitHub data,
TWRP / PitchBlack / Xiaomi.eu / OrangeFox download sites."""

import re
import html
import time
import asyncio
import difflib
import logging
import xml.etree.ElementTree as ET
from itertools import groupby
from collections import defaultdict

import httpx
import yaml
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

log = logging.getLogger("proai.xiaomi")

GH = "https://raw.githubusercontent.com/XiaomiFirmwareUpdater"
SITE = "https://www.xmfirmwareupdater.com"
WIKI = "https://xiaomiwiki.github.io/wiki"
YLOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
esc = lambda x: html.escape(str(x if x is not None else "-"))

# ───────────────────────── fetch + cache ─────────────────────────
_cache, _locks = {}, defaultdict(asyncio.Lock)


async def fetch(url, timeout=60):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "ProAI-Bot/1.0"}) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


async def cached(key, ttl, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with _locks[key]:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await loader()
        except Exception:
            if hit:  # purana data de do agar naya fail ho
                return hit[1]
            raise
        _cache[key] = (time.time(), val)
        return val


async def load_yaml(url):
    return await asyncio.to_thread(yaml.load, await fetch(url), Loader=YLOADER)


async def load_json(url):
    import json
    return await asyncio.to_thread(json.loads, await fetch(url))


# ───────────────────────── data loaders ─────────────────────────
async def names():  # codename -> device name
    return await cached("names", 21600, lambda: load_yaml(f"{GH}/xiaomifirmwareupdater.github.io/master/data/names.yml"))


async def miui_codenames():
    return set(await cached("miui_cn", 21600, lambda: load_yaml(
        f"{GH}/xiaomifirmwareupdater.github.io/master/data/miui_codenames.yml")))


async def miui_roms():
    async def load():
        data = await load_yaml(f"{GH}/miui-updates-tracker/master/data/latest.yml")
        out = defaultdict(list)
        for i in data:
            out[str(i["codename"]).split("_")[0]].append({
                k: (str(i.get(k)) if i.get(k) is not None else None)
                for k in ("name", "branch", "method", "version", "android", "link", "size", "date")})
        return dict(out)
    return await cached("miui_roms", 1800, load)


async def firmware_data():
    async def load():
        data = await load_yaml(f"{GH}/xiaomifirmwareupdater.github.io/master/data/devices/latest.yml")
        out = defaultdict(list)
        for i in data:
            try:
                cn = i["downloads"]["github"].split("/")[4].split("_")[-1]
            except Exception:
                continue
            out[cn].append({"region": i.get("region"), "branch": i.get("branch"), "date": str(i.get("date")),
                            "miui": i["versions"]["miui"], "android": i["versions"].get("android"),
                            "link": i["downloads"]["github"]})
        return dict(out)
    return await cached("fw", 3600, load)


async def vendor_data():
    async def load():
        data = await load_yaml(f"{GH}/xiaomifirmwareupdater.github.io/master/data/vendor/latest.yml")
        out = defaultdict(list)
        for i in data:
            try:
                cn = i["downloads"]["github"].split("/")[7].split("_")[0].split("-")[0]
            except Exception:
                continue
            out[cn].append({"branch": i.get("branch"), "date": str(i.get("date")),
                            "miui": i["versions"]["miui"], "android": i["versions"].get("android"),
                            "link": i["downloads"]["github"]})
        return dict(out)
    return await cached("vendor", 3600, load)


async def models():
    return await cached("models", 21600, lambda: load_json(f"{GH}/xiaomi_devices/models/models.json"))


async def specs_data():
    return await cached("specs", 21600, lambda: load_json(f"{GH}/xiaomi_devices/gsmarena/devices.json"))


async def eu_codenames():
    return await cached("eu_cn", 21600, lambda: load_json(f"{GH}/xiaomi_devices/eu/devices.json"))


async def sf_links(rss_url):
    root = ET.fromstring(await fetch(rss_url))
    return [i.find("link").text for i in root[0].findall("item")]


async def eu_links():
    base = "https://sourceforge.net/projects/xiaomi-eu-multilang-miui-roms/rss?path=/xiaomi.eu"

    async def load():
        out = []
        for d in ("HyperOS-STABLE-RELEASES", "HyperOS-WEEKLY-RELEASES",
                  "MIUI-STABLE-RELEASES", "MIUI-WEEKLY-RELEASES"):
            try:
                out += await sf_links(f"{base}/{d}")
            except Exception:
                log.exception("eu rss %s", d)
        return out
    return await cached("eu_links", 3600, load)


async def pb_links():
    return await cached("pb", 21600, lambda: sf_links("https://sourceforge.net/projects/pitchblack-twrp/rss?path=/"))


async def twrp_devices():
    async def load():
        page = BeautifulSoup(await fetch("https://twrp.me/Devices/Xiaomi/"), "html.parser")
        out = {}
        for a in page.find("ul", {"class": "post-list"}).findAll("a"):
            cn = a.text.split("(")[-1].split(")")[0].split("/")[0]
            out[cn] = {"name": a.text, "link": f"https://dl.twrp.me/{cn}/"}
        return out
    return await cached("twrp", 21600, load)


# ───────────────────────── helpers ─────────────────────────
def kb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, url=u) for t, u in row] for row in rows])


async def device_arg(update, ctx, db_get):
    """Codename: command argument ya /set_codename wala default."""
    if ctx.args:
        return ctx.args[0].lower()
    d = db_get(update.effective_chat.id, "codename")
    if d:
        return d
    await update.effective_message.reply_text(
        "Codename do, jaise <code>/twrp whyred</code>\n"
        "Ya default set karo: <code>/set_codename whyred</code>\n"
        "Codename nahi pata? <code>/codename redmi note 5</code>", parse_mode=ParseMode.HTML)
    return None


async def bad_codename(update, device, pool):
    sug = difflib.get_close_matches(device, list(pool), n=4, cutoff=0.6)
    txt = f"❌ <code>{esc(device)}</code> naam ka koi device nahi mila."
    if sug:
        txt += "\nShayad ye: " + ", ".join(f"<code>{s}</code>" for s in sug)
    txt += "\nNaam se codename: /codename <name>"
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.HTML)


async def send(update, text, markup=None):
    await update.effective_message.reply_text(text[:4000], parse_mode=ParseMode.HTML,
                                              reply_markup=markup, disable_web_page_preview=True)


def safe(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await fn(update, ctx)
        except Exception as e:
            log.exception("xiaomi cmd failed")
            await update.effective_message.reply_text(f"⚠️ Data abhi nahi mila ({type(e).__name__}). Thodi der baad try karo.")
    return wrapper


# ───────────────────────── MIUI / HyperOS ROMs ─────────────────────────
def pick_roms(device_roms, method):
    """Uranus ka logic: har variant ke liye latest stable/beta/weekly."""
    items = [i for i in device_roms if i["method"] == method]
    out = []
    for _, grp in groupby(sorted(items, key=lambda x: x["name"] or ""), lambda x: x["name"]):
        grp = list(grp)
        by = lambda b: [x for x in grp if x["branch"] == b]
        for b in ("Stable", "Stable Beta", "Public Beta", "Weekly"):
            if by(b):
                out.append(by(b)[0])
    return out


def build_rom_text(device, devname, roms, with_links):
    lines = [f"<b>{esc(devname)}</b> (<code>{device}</code>)"]
    last = None
    for r in roms:
        if r["name"] != last:
            lines.append(f"\n<b>{esc(r['name'])}</b>")
            last = r["name"]
        line = f"• {esc(r['branch'])}: <code>{esc(r['version'])}</code> | Android {esc(r['android'])}"
        if with_links:
            line += f" | {esc(r['size'])} | {esc(r['date'])}\n  <a href=\"{esc(r['link'])}\">⬇️ Download</a>"
        lines.append(line)
        if sum(len(x) for x in lines) > 3500:
            lines.append("\n… aur variants: archive pe dekho")
            break
    return "\n".join(lines)


def make_miui_cmd(method, with_links, db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        cns = await miui_codenames()
        if device not in cns:
            return await bad_codename(update, device, cns)
        roms = (await miui_roms()).get(device, [])
        sel = pick_roms(roms, method)
        if not sel:
            return await send(update, f"Is device ke liye {method} ROM nahi mila 🤷")
        nm = (await names()).get(device, device)
        await send(update, build_rom_text(device, nm, sel, with_links),
                   kb([("📚 MIUI archive", f"{SITE}/archive/miui/{device}/"),
                       ("📚 HyperOS archive", f"{SITE}/archive/hyperos/{device}/")]))
    return cmd


def make_archive(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        cns = await miui_codenames()
        if device not in cns:
            return await bad_codename(update, device, cns)
        nm = (await names()).get(device, device)
        await send(update, f"📚 <b>{esc(nm)}</b> (<code>{device}</code>)\nSaare official ROMs ka archive:",
                   kb([("MIUI", f"{SITE}/archive/miui/{device}/"), ("HyperOS", f"{SITE}/archive/hyperos/{device}/")]))
    return cmd


# ───────────────────────── Firmware / Vendor ─────────────────────────
def make_fw_cmd(kind, db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        data = await (firmware_data() if kind == "firmware" else vendor_data())
        if device not in data:
            return await bad_codename(update, device, data.keys())
        nm = (await names()).get(device, device)
        lines = [f"<b>{'Firmware' if kind == 'firmware' else 'Vendor'}</b> — {esc(nm)} (<code>{device}</code>)"]
        for i in data[device][:6]:
            region = f"{esc(i.get('region'))} | " if i.get("region") else ""
            lines.append(f"\n• {region}{esc(i['branch'])}: <code>{esc(i['miui'])}</code> | {esc(i['date'])}"
                         f"\n  <a href=\"{esc(i['link'])}\">⬇️ Download</a>")
        await send(update, "\n".join(lines),
                   kb([("🌐 Latest", f"{SITE}/{kind}/{device}/"), ("📚 Archive", f"{SITE}/archive/{kind}/{device}/")]))
    return cmd


# ───────────────────────── Custom recoveries / EU ─────────────────────────
def make_twrp(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        devs = await twrp_devices()
        if device not in devs:
            return await bad_codename(update, device, devs.keys())
        link = devs[device]["link"]
        page = BeautifulSoup(await fetch(link), "html.parser").find("table").find("tr")
        a = page.find("a")
        size = page.find("span", {"class": "filesize"}).text
        date = page.find("em").text.strip()
        await send(update, f"<b>TWRP</b> — {esc(devs[device]['name'])}\n<code>{esc(a.text)}</code>\n"
                           f"Size: {esc(size)} | {esc(date)}",
                   kb([("⬇️ Download", f"https://dl.twrp.me{a['href']}"), ("All builds", link)]))
    return cmd


def make_pb(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        links = [i for i in await pb_links() if device in i]
        if not links:
            return await send(update, f"PitchBlack ka <code>{esc(device)}</code> ke liye build nahi mila 🤷")
        await send(update, f"<b>PitchBlack Recovery</b> — <code>{esc(device)}</code>\n{esc(links[0].split('/')[-2])}",
                   kb([("⬇️ Download", links[0]),
                       ("All builds", "https://sourceforge.net/projects/pitchblack-twrp/files/")]))
    return cmd


def make_of(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        page = f"https://orangefox.download/device/{device}"
        rows, info = [], f"<b>OrangeFox</b> — <code>{esc(device)}</code>"
        try:
            api = "https://api.orangefox.download/v3"
            d = await load_json(f"{api}/devices/get?codename={device}")
            info = f"<b>OrangeFox</b> — {esc(d.get('full_name', device))} (<code>{esc(device)}</code>)"
            if d.get("maintainer"):
                info += f"\nMaintainer: {esc(d['maintainer'].get('name'))}"
            for typ in ("stable", "beta"):
                rl = await load_json(f"{api}/releases/?device_id={d['_id']}&type={typ}&limit=1")
                if rl.get("data"):
                    rel = await load_json(f"{api}/releases/get?_id={rl['data'][0]['_id']}")
                    rows.append([(f"⬇️ {typ.title()}: {rel['filename']}", rel["url"])])
        except Exception:
            log.exception("orangefox api")
        rows.append([("🦊 OrangeFox page", page)])
        await send(update, info, kb(*rows))
    return cmd


def make_eu(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        devs = await eu_codenames()
        if device not in devs:
            return await bad_codename(update, device, devs.keys())
        code = devs[device][1]
        links = await eu_links()
        stable = [i for i in links if re.search(rf"{re.escape(code)}_(?:V|OS)", i)
                  and not re.search(rf"{re.escape(code)}_(?:V|OS).*DEV", i)]
        weekly = [i for i in links if re.search(rf"{re.escape(code)}_(?:V|OS).*DEV", i)]
        rows = []
        if stable:
            rows.append([(f"⬇️ Stable: {stable[0].split('/')[-2][:40]}", stable[0])])
        if weekly:
            rows.append([(f"⬇️ Weekly: {weekly[0].split('/')[-2][:40]}", weekly[0])])
        rows.append([("Xiaomi.eu ROMs", "https://xiaomi.eu/community/link-forums/roms-download.73/")])
        await send(update, f"<b>Xiaomi.eu ROMs</b> — {esc(devs[device][0])} (<code>{esc(device)}</code>)"
                           + ("" if stable or weekly else "\nAbhi koi build nahi mila 🤷"), kb(*rows))
    return cmd


# ───────────────────────── Device info ─────────────────────────
@safe
async def whatis(update, ctx):
    if not ctx.args:
        return await send(update, "Use: <code>/whatis whyred</code>")
    cn = ctx.args[0].lower()
    nm = await names()
    if cn not in nm:
        return await bad_codename(update, cn, nm.keys())
    await send(update, f"<code>{esc(cn)}</code> = <b>{esc(nm[cn])}</b>")


@safe
async def codename(update, ctx):
    if not ctx.args:
        return await send(update, "Use: <code>/codename redmi note 5</code>")
    q = " ".join(ctx.args).lower()
    hits = {n: c for c, n in (await names()).items()
            if n.lower().startswith(q) or any(p.strip().lower().startswith(q) for p in n.split("/"))}
    if not hits:
        return await send(update, "Koi device nahi mila 🤷")
    if len(hits) > 15:
        return await send(update, "Bahut saare results hain, naam thoda aur specific likho.")
    await send(update, "\n".join(f"{esc(n)}: <code>{esc(c)}</code>" for n, c in hits.items()))


def make_models(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        m = await models()
        if device not in m:
            return await bad_codename(update, device, m.keys())
        d = m[device]
        body = "\n".join(f"<code>{esc(k.strip(chr(96)))}</code>: {esc(v)}" for k, v in d["models"].items())
        await send(update, f"<b>{esc(d['name'])}</b> (<code>{esc(device)}</code>) — {esc(d['internal_name'])}\n\n{body}")
    return cmd


def make_specs(db_get):
    @safe
    async def cmd(update, ctx):
        device = await device_arg(update, ctx, db_get)
        if not device:
            return
        hits = [i for i in await specs_data() if device in i["codenames"]]
        if not hits:
            return await send(update, f"<code>{esc(device)}</code> ke specs nahi mile 🤷")
        out = []
        for i in hits[:2]:
            s = i["specs"]
            g = lambda sec, key: (s.get(sec) or {}).get(key, "-")
            cam = lambda sec: next(iter((s.get(sec) or {"-": "-"}).items()))
            out.append(
                f"<a href=\"{esc(i['url'])}\">{esc(i['name'])}</a> — <b>{esc(device)}</b>\n"
                f"<b>Status:</b> {esc(g('Launch', 'Status'))}\n<b>Network:</b> {esc(g('Network', 'Technology'))}\n"
                f"<b>Weight:</b> {esc(g('Body', 'Weight'))}\n"
                f"<b>Display:</b> {esc(g('Display', 'Type'))}, {esc(g('Display', 'Size'))}, {esc(g('Display', 'Resolution'))}\n"
                f"<b>Chipset:</b> {esc(g('Platform', 'Chipset'))}\n<b>CPU:</b> {esc(g('Platform', 'CPU'))}\n"
                f"<b>GPU:</b> {esc(g('Platform', 'GPU'))}\n<b>Memory:</b> {esc(g('Memory', 'Internal'))}\n"
                f"<b>Rear cam:</b> {esc(' '.join(cam('Main Camera')))}\n<b>Front cam:</b> {esc(' '.join(cam('Selfie camera')))}\n"
                f"<b>3.5mm jack:</b> {esc(g('Sound', '3.5mm jack'))}\n<b>USB:</b> {esc(g('Comms', 'USB'))}\n"
                f"<b>Sensors:</b> {esc(g('Features', 'Sensors'))}\n<b>Battery:</b> {esc(g('Battery', 'Type'))}"
                + (f"\n<b>Charging:</b> {esc(g('Battery', 'Charging'))}" if g('Battery', 'Charging') != "-" else ""))
        await send(update, "\n\n".join(out))
    return cmd


async def unlockbl(update, ctx):
    await send(update, "🔓 <b>Bootloader unlock</b>",
               kb([("How to unlock", f"{WIKI}/Unlock_the_bootloader.html")],
                  [("Mi Unlock Tool", "http://en.miui.com/unlock/download_en.html")]))


async def tools(update, ctx):
    u = f"{WIKI}/Tools_for_Xiaomi_devices.html"
    await send(update, "🛠 <b>Xiaomi tools</b>", kb(
        [("Mi Flash Tool", f"{u}#miflash-by-xiaomi"), ("MiFlash Pro", f"{u}#miflash-pro-by-xiaomi")],
        [("Mi Unlock Tool", f"{u}#miunlock-by-xiaomi"), ("XiaomiTool", f"{u}#xiaomitool-v2-by-francesco-tescari")],
        [("Xiaomi ADB/Fastboot Tools", f"{u}#xiaomi-adbfastboot-tools-by-saki_eu"), ("More tools", u)]))


async def guides(update, ctx):
    await send(update, "📖 <b>Xiaomi guides</b>", kb(
        [("Flashing official ROMs", f"{WIKI}/Flash_official_ROMs.html")],
        [("Flashing TWRP & custom ROMs", f"{WIKI}/Flash_TWRP_and_custom_ROMs.html")],
        [("Fix notifications on MIUI", f"{WIKI}/Fix_notifications_on_MIUI.html")],
        [("Disable MIUI ads", f"{WIKI}/Disable_ads_in_MIUI.html")]))


def register(app, db_get, db_set):
    """Saare Xiaomi commands register karo."""
    async def set_codename(update, ctx):
        if not ctx.args:
            return await send(update, "Use: <code>/set_codename whyred</code>")
        cn = ctx.args[0].lower()
        nm = await names()
        if cn not in nm:
            return await bad_codename(update, cn, nm.keys())
        db_set(update.effective_chat.id, "codename", cn)
        await send(update, f"✅ Default device: <b>{esc(nm[cn])}</b> (<code>{cn}</code>)")

    cmds = {
        "recovery": make_miui_cmd("Recovery", True, db_get),
        "fastboot": make_miui_cmd("Fastboot", True, db_get),
        "latest": make_miui_cmd("Recovery", False, db_get),
        "archive": make_archive(db_get),
        "firmware": make_fw_cmd("firmware", db_get),
        "vendor": make_fw_cmd("vendor", db_get),
        "twrp": make_twrp(db_get), "pb": make_pb(db_get), "of": make_of(db_get), "eu": make_eu(db_get),
        "specs": make_specs(db_get), "models": make_models(db_get),
        "whatis": whatis, "codename": codename, "set_codename": safe(set_codename),
        "unlockbl": unlockbl, "tools": tools, "guides": guides,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))


# ───────────────────────── Mini App data API ─────────────────────────
async def search_devices(q, limit=30):
    q = (q or "").lower().strip()
    if len(q) < 2:
        return []
    scored = []
    for cn, nm in (await names()).items():
        low = nm.lower()
        parts = [p.strip() for p in low.split("/")]
        if q == cn:
            s = 0
        elif cn.startswith(q):
            s = 1
        elif any(p.startswith(q) for p in parts):
            s = 2
        elif q in low or q in cn:
            s = 3
        else:
            continue
        scored.append((s, nm, cn))
    scored.sort()
    return [{"codename": cn, "name": nm} for _, nm, cn in scored[:limit]]


def _rom_rows(roms, method):
    return [{k: r[k] for k in ("name", "branch", "version", "android", "size", "date", "link")}
            for r in pick_roms(roms, method)]


def _spec_summary(i, device):
    s = i["specs"]
    g = lambda sec, key: (s.get(sec) or {}).get(key) or "-"
    first = lambda sec: " ".join(next(iter((s.get(sec) or {"-": "-"}).items())))
    rows = [("Status", g("Launch", "Status")), ("Network", g("Network", "Technology")),
            ("Display", f"{g('Display', 'Type')}, {g('Display', 'Size')}"), ("Resolution", g("Display", "Resolution")),
            ("Chipset", g("Platform", "Chipset")), ("CPU", g("Platform", "CPU")), ("GPU", g("Platform", "GPU")),
            ("Memory", g("Memory", "Internal")), ("Rear camera", first("Main Camera")),
            ("Front camera", first("Selfie camera")), ("Battery", g("Battery", "Type")),
            ("Charging", g("Battery", "Charging")), ("Weight", g("Body", "Weight")),
            ("USB", g("Comms", "USB")), ("3.5mm jack", g("Sound", "3.5mm jack")),
            ("Sensors", g("Features", "Sensors"))]
    return {"name": i["name"], "url": i["url"], "rows": [{"k": k, "v": v} for k, v in rows if v != "-"]}


async def device_detail(cn):
    nm = await names()
    if cn not in nm:
        return None
    res = await asyncio.gather(miui_roms(), firmware_data(), vendor_data(), models(), specs_data(),
                               return_exceptions=True)
    ok = lambda r, d: d if isinstance(r, Exception) else r
    roms, fw, vd, md, sp = (ok(res[0], {}), ok(res[1], {}), ok(res[2], {}), ok(res[3], {}), ok(res[4], []))
    dev_roms = roms.get(cn, [])
    return {
        "codename": cn, "name": nm[cn],
        "recovery": _rom_rows(dev_roms, "Recovery"), "fastboot": _rom_rows(dev_roms, "Fastboot"),
        "firmware": [{k: i.get(k) for k in ("region", "branch", "miui", "date", "link")} for i in fw.get(cn, [])[:6]],
        "vendor": [{k: i.get(k) for k in ("branch", "miui", "date", "link")} for i in vd.get(cn, [])[:4]],
        "specs": [_spec_summary(i, cn) for i in sp if cn in i.get("codenames", [])][:2],
        "models": [{"model": k.strip("`"), "name": v} for k, v in (md.get(cn, {}).get("models", {})).items()],
        "links": {"miui": f"{SITE}/archive/miui/{cn}/", "hyperos": f"{SITE}/archive/hyperos/{cn}/",
                  "firmware": f"{SITE}/firmware/{cn}/", "vendor": f"{SITE}/vendor/{cn}/"},
    }


async def _twrp_info(cn):
    devs = await twrp_devices()
    if cn not in devs:
        return None
    link = devs[cn]["link"]
    row = BeautifulSoup(await fetch(link), "html.parser").find("table").find("tr")
    a = row.find("a")
    return {"name": devs[cn]["name"], "file": a.text, "url": f"https://dl.twrp.me{a['href']}",
            "size": row.find("span", {"class": "filesize"}).text, "date": row.find("em").text.strip(), "page": link}


async def _pb_info(cn):
    links = [i for i in await pb_links() if cn in i]
    if not links:
        return None
    return {"file": links[0].split("/")[-2], "url": links[0],
            "page": "https://sourceforge.net/projects/pitchblack-twrp/files/"}


async def _of_info(cn):
    api = "https://api.orangefox.download/v3"
    d = await load_json(f"{api}/devices/get?codename={cn}")
    out = {"name": d.get("full_name", cn), "maintainer": (d.get("maintainer") or {}).get("name"),
           "page": f"https://orangefox.download/device/{cn}", "downloads": []}
    for typ in ("stable", "beta"):
        rl = await load_json(f"{api}/releases/?device_id={d['_id']}&type={typ}&limit=1")
        if rl.get("data"):
            rel = await load_json(f"{api}/releases/get?_id={rl['data'][0]['_id']}")
            out["downloads"].append({"type": typ, "file": rel["filename"], "url": rel["url"]})
    return out


async def _eu_info(cn):
    devs = await eu_codenames()
    if cn not in devs:
        return None
    code = re.escape(devs[cn][1])
    links = await eu_links()
    weekly = [i for i in links if re.search(rf"{code}_(?:V|OS).*DEV", i)]
    stable = [i for i in links if re.search(rf"{code}_(?:V|OS)", i) and i not in weekly]
    pick = lambda l: ({"file": l[0].split("/")[-2], "url": l[0]} if l else None)
    return {"stable": pick(stable), "weekly": pick(weekly)}


async def recoveries(cn):
    res = await asyncio.gather(_twrp_info(cn), _pb_info(cn), _of_info(cn), _eu_info(cn), return_exceptions=True)
    for r in res:
        if isinstance(r, Exception):
            log.warning("recovery lookup failed: %r", r)
    ok = lambda r: None if isinstance(r, Exception) else r
    return {"twrp": ok(res[0]), "pitchblack": ok(res[1]), "orangefox": ok(res[2]), "eu": ok(res[3])}
