#!/usr/bin/env python3
"""OVHcloud dedicated-server stock watcher -> Discord webhook (+ optional ntfy). Stdlib only."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENDPOINTS = {"us": ("US", "https://api.us.ovhcloud.com/1.0"), "ca": ("CA", "https://ca.api.ovh.com/1.0")}
DC_NAMES = {"bhs": "Beauharnois, QC", "hil": "Hillsboro, OR", "vin": "Vint Hill, VA", "tor": "Toronto",
            "gra": "Gravelines", "rbx": "Roubaix", "sbg": "Strasbourg", "lon": "London", "fra": "Frankfurt",
            "waw": "Warsaw", "syd": "Sydney", "sgp": "Singapore", "ynm": "Mumbai"}
NOT_ORDERABLE = {"unavailable", "comingsoon"}
UA = "ovh-stock-watch/1.0"


def log(level: str, msg: str, **kv) -> None:
    def q(v):
        s = str(v)
        return json.dumps(s) if not s or any(c in s for c in ' ="') else s
    parts = [f"ts={datetime.now(timezone.utc).isoformat(timespec='seconds')}", f"level={level}", f"msg={json.dumps(msg)}"]
    print(" ".join(parts + [f"{k}={q(v)}" for k, v in kv.items()]), flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


class Config:
    def __init__(self) -> None:
        self.plan_codes = csv(env("PLAN_CODES"))
        # Alternative to PLAN_CODES: regex on catalog name/CPU, resolved to plan codes at runtime.
        self.plan_match = re.compile(env("PLAN_MATCH"), re.I) if env("PLAN_MATCH") else None
        self.datacenters = [d.lower() for d in csv(env("DATACENTERS", "bhs,hil,vin"))]
        self.endpoints: list[tuple[str, str]] = []  # (subsidiary, base_url)
        for e in csv(env("OVH_ENDPOINTS", "us")):
            if e.lower() in ENDPOINTS:
                self.endpoints.append(ENDPOINTS[e.lower()])
            elif "=" in e:  # custom: SUB=https://host/1.0
                sub, url = e.split("=", 1)
                self.endpoints.append((sub.upper(), url.rstrip("/")))
            else:
                sys.exit(f"Unknown OVH_ENDPOINTS entry {e!r} (use us, ca, or SUB=https://...)")
        self.storage_inc = re.compile(env("STORAGE_INCLUDE", "nvme|ssd"), re.I)
        self.storage_exc = re.compile(env("STORAGE_EXCLUDE", "hdd|sata"), re.I)
        self.memory_inc = re.compile(env("MEMORY_INCLUDE"), re.I) if env("MEMORY_INCLUDE") else None
        self.poll = max(30, int(env("POLL_SECONDS", "180")))
        self.state_path = env("STATE_PATH", "./state.json")
        self.webhook = env("DISCORD_WEBHOOK_URL")
        self.mention = env("DISCORD_MENTION")
        self.order_url = env("ORDER_URL")
        self.label = env("ALERT_LABEL", "KS-6 NVMe")
        self.ntfy_topic = env("NTFY_TOPIC")
        self.ntfy_server = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_token = env("NTFY_TOKEN")
        self.heartbeat = env("HEARTBEAT_TIME", "09:00")
        if self.heartbeat.lower() in ("off", "none", "disabled"):
            self.heartbeat = ""
        elif not re.fullmatch(r"\d{1,2}:\d{2}", self.heartbeat):
            sys.exit(f"HEARTBEAT_TIME must be HH:MM or 'off', got {self.heartbeat!r}")


# ---------------------------------------------------------------- HTTP / OVH API
class ApiError(Exception):
    pass


def http(method: str, url: str, body=None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json", **({"Content-Type": "application/json"} if data else {})}
    req = urllib.request.Request(url, data=data, headers={**h, **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def get_json(url: str):
    try:
        return http("GET", url)
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} {e.reason} from {url}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ApiError(f"{type(e).__name__}: {e} ({url})") from e


def fetch_availability(base: str, plan_codes: list[str] | None = None) -> list[dict]:
    url = f"{base}/dedicated/server/datacenter/availabilities"
    if not plan_codes:
        return get_json(url) or []
    out: list[dict] = []
    for pc in plan_codes:
        out += get_json(f"{url}?{urllib.parse.urlencode({'planCode': pc})}") or []
    return out


def fetch_catalog(sub: str, base: str) -> tuple[dict, dict]:
    cat = get_json(f"{base}/order/catalog/public/eco?{urllib.parse.urlencode({'ovhSubsidiary': sub})}") or {}
    return ({p.get("planCode"): p for p in cat.get("plans", [])}, {a.get("planCode"): a for a in cat.get("addons", [])})


def dig(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def describe(plans: dict, plan_code: str) -> str:
    p = plans.get(plan_code)
    if not p:
        return ""
    name = p.get("invoiceName") or dig(p, "blobs", "commercial", "name") or plan_code
    cpu = dig(p, "blobs", "technical", "server", "cpu") or {}
    model = str(cpu.get("model") or "")
    if model and model.lower() not in name.lower():
        name += f" ({cpu.get('brand', '')} {model})".replace("( ", "(")
    return name


def resolve_plans(cfg: "Config", catalog: dict) -> None:
    """With PLAN_MATCH set, (re)derive cfg.plan_codes from the cached catalogs."""
    if cfg.plan_match:
        found = sorted({pc for plans, _ in catalog.values() for pc in plans if cfg.plan_match.search(describe(plans, pc))})
        if found != cfg.plan_codes:
            log("INFO" if found else "WARN", "PLAN_MATCH resolved plan codes", pattern=cfg.plan_match.pattern,
                plans=",".join(found) or "NONE", names=" / ".join(sorted({describe(p, pc) for p, _ in catalog.values() for pc in found if pc in p})))
        cfg.plan_codes = found


def disk_type(addons: dict, storage: str, plan_code: str) -> str:
    disks = dig(addons.get(f"{storage}-{plan_code}") or addons.get(storage), "blobs", "technical", "storage", "disks") or []
    return "+".join(sorted({str(d.get("technology")) for d in disks if d.get("technology")}))


# ---------------------------------------------------------------- matching
def is_orderable(av: str) -> bool:
    return bool(av) and av.lower() not in NOT_ORDERABLE


def config_ok(cfg: Config, it: dict) -> bool:
    st, mem = it.get("storage") or "", it.get("memory") or ""
    return bool(cfg.storage_inc.search(st)) and not cfg.storage_exc.search(st) and \
        (cfg.memory_inc is None or bool(cfg.memory_inc.search(mem)))


def check(cfg: Config) -> dict[str, dict]:
    """One row per watched (subsidiary, fqn, datacenter). Raises ApiError on any fetch failure."""
    rows: dict[str, dict] = {}
    for sub, base in cfg.endpoints:
        for it in fetch_availability(base, cfg.plan_codes):
            if it.get("planCode") not in cfg.plan_codes or not config_ok(cfg, it):
                continue
            fqn = it.get("fqn") or ".".join(str(it.get(k)) for k in ("planCode", "memory", "storage"))
            for d in it.get("datacenters") or []:
                dc = str(d.get("datacenter") or "").lower()
                if dc in cfg.datacenters:
                    av = d.get("availability") or "unavailable"
                    rows[f"{sub}|{fqn}|{dc}"] = {"sub": sub, "fqn": fqn, "plan": it.get("planCode"), "server": it.get("server"),
                                                 "memory": it.get("memory"), "storage": it.get("storage"), "dc": dc,
                                                 "availability": av, "available": is_orderable(av)}
    return rows


# ---------------------------------------------------------------- notifiers
def post_discord(cfg: Config, embed: dict | None = None, content: str = "", mention: bool = False) -> bool:
    if not cfg.webhook:
        return False
    payload: dict = {"embeds": [embed] if embed else []}
    text = " ".join(x for x in ((cfg.mention if mention else ""), content) if x)
    if text:
        payload["content"] = text
    for attempt in range(3):
        try:
            http("POST", cfg.webhook + ("&" if "?" in cfg.webhook else "?") + "wait=true", payload)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(min(30.0, float(e.headers.get("Retry-After") or 2)))
                continue
            log("ERROR", "discord post failed", status=e.code, body=e.read()[:300].decode(errors="replace"))
            return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            log("ERROR", "discord post failed", error=e)
            return False
    return False


def post_ntfy(cfg: Config, title: str, message: str, priority: int, tags: list[str]) -> bool:
    if not cfg.ntfy_topic:
        return False
    body = {"topic": cfg.ntfy_topic, "title": title, "message": message, "priority": priority, "tags": tags}
    if cfg.order_url:
        body["click"] = cfg.order_url
    try:
        http("POST", cfg.ntfy_server, body, {"Authorization": f"Bearer {cfg.ntfy_token}"} if cfg.ntfy_token else None)
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("ERROR", "ntfy post failed", error=e)
        return False


def dc_label(dc: str) -> str:
    return f"{DC_NAMES.get(dc, dc.upper())} ({dc})"


def fmt_duration(sec: float) -> str:
    m = int(sec) // 60
    d, rem = divmod(m, 1440)
    h, m = divmod(rem, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m")) if v) or "<1m"


def notify_in_stock(cfg: Config, row: dict, desc: str, label: str | None = None) -> bool:
    now = int(time.time())
    title = f"🟢 {label or cfg.label} in stock — {DC_NAMES.get(row['dc'], row['dc'].upper())}"
    embed = {"title": title, "color": 0x2ECC71, "timestamp": datetime.now(timezone.utc).isoformat(), "fields": [
        {"name": "Plan code", "value": f"`{row['plan']}`", "inline": True},
        {"name": "CPU / RAM", "value": f"{desc or row['server']}\n`{row['memory']}`", "inline": True},
        {"name": "Storage", "value": f"`{row['storage']}`", "inline": True},
        {"name": "Datacenter", "value": dc_label(row["dc"]), "inline": True},
        {"name": "Availability", "value": f"`{row['availability']}`", "inline": True},
        {"name": "Detected", "value": f"<t:{now}:F> (<t:{now}:R>)", "inline": True}],
        "footer": {"text": f"{row['fqn']} · {row['sub']} API"}}
    if cfg.order_url:
        embed["url"] = cfg.order_url
        embed["description"] = f"**[Order now →]({cfg.order_url})**"
    d = post_discord(cfg, embed, mention=True)
    n = post_ntfy(cfg, title, f"{row['fqn']} @ {dc_label(row['dc'])}, delay {row['availability']}", 4, ["green_circle"])
    return d or n


def notify_out_of_stock(cfg: Config, row: dict, lasted: float) -> None:
    title = f"🔴 {cfg.label} out of stock again — {DC_NAMES.get(row['dc'], row['dc'].upper())}"
    embed = {"title": title, "color": 0xE74C3C, "timestamp": datetime.now(timezone.utc).isoformat(), "fields": [
        {"name": "Plan code", "value": f"`{row['plan']}`", "inline": True},
        {"name": "Storage", "value": f"`{row['storage']}`", "inline": True},
        {"name": "Datacenter", "value": dc_label(row["dc"]), "inline": True},
        {"name": "Was in stock for", "value": fmt_duration(lasted), "inline": True}],
        "footer": {"text": f"{row['fqn']} · {row['sub']} API"}}
    post_discord(cfg, embed)
    post_ntfy(cfg, title, f"{row['fqn']} gone after {fmt_duration(lasted)}", 3, ["red_circle"])


# ---------------------------------------------------------------- state & loop
def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log("WARN", "state file unreadable, starting fresh", path=path, error=e)
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(path + ".tmp", path)


def process(cfg: Config, state: dict, rows: dict, catalog: dict) -> None:
    items, now = state.setdefault("items", {}), time.time()
    for key, row in rows.items():
        prev = items.get(key) or {}
        if row["available"] and not prev.get("available"):
            log("INFO", "IN STOCK", key=key, availability=row["availability"])
            if notify_in_stock(cfg, row, describe(catalog.get(row["sub"], ({}, {}))[0], row["plan"])):
                items[key] = {"available": True, "since": now, "row": row}
            else:
                log("ERROR", "alert not delivered, will retry next poll", key=key)
        elif row["available"]:
            prev["row"] = row
        elif prev.get("available"):
            log("INFO", "out of stock again", key=key, lasted=fmt_duration(now - prev["since"]))
            notify_out_of_stock(cfg, row, now - prev["since"])
            items[key] = {"available": False, "since": now}
        elif not prev:
            items[key] = {"available": False, "since": now}
    for key in [k for k in items if k not in rows]:  # config vanished from API response = gone
        prev = items.pop(key)
        r = prev.get("row") or {}
        if prev.get("available") and r.get("plan") in cfg.plan_codes and r.get("dc") in cfg.datacenters:
            notify_out_of_stock(cfg, r, now - prev["since"])


def maybe_heartbeat(cfg: Config, state: dict, watched: int) -> None:
    if not cfg.heartbeat:
        return
    hh, mm = map(int, cfg.heartbeat.split(":"))
    now = datetime.now()  # local time; set TZ in the container
    if state.get("last_heartbeat") == now.date().isoformat() or (now.hour, now.minute) < (hh, mm):
        return
    ok = state.get("last_ok")
    msg = (f"💓 Stock watcher alive, last check OK <t:{int(ok)}:R>, " if ok else "💓 Stock watcher alive, NO successful check yet, ") \
        + f"{watched} configs watched ({', '.join(cfg.plan_codes)} @ {', '.join(cfg.datacenters)})"
    sent = post_discord(cfg, content=msg) if cfg.webhook else post_ntfy(cfg, "Stock watcher alive", msg, 2, ["heartbeat"])
    if sent:
        state["last_heartbeat"] = now.date().isoformat()
        log("INFO", "heartbeat sent")


def run(cfg: Config) -> None:
    if not (cfg.plan_codes or cfg.plan_match):
        sys.exit("Set PLAN_CODES (find it with --list) or PLAN_MATCH.")
    if not (cfg.webhook or cfg.ntfy_topic):
        sys.exit("Set DISCORD_WEBHOOK_URL and/or NTFY_TOPIC.")
    state = load_state(cfg.state_path)
    catalog: dict = {}
    catalog_at, fails, first_fail, last_err_post, watched = 0.0, 0, 0.0, 0.0, 0
    log("INFO", "watcher started", plans=",".join(cfg.plan_codes), dcs=",".join(cfg.datacenters),
        endpoints=",".join(s for s, _ in cfg.endpoints), poll=cfg.poll)
    while True:
        now = time.time()
        if now - catalog_at > 86400 or (cfg.plan_match and not cfg.plan_codes):  # names + PLAN_MATCH; daily refresh
            for sub, base in cfg.endpoints:
                try:
                    catalog[sub] = fetch_catalog(sub, base)
                    catalog_at = now
                except ApiError as e:
                    log("WARN", "catalog fetch failed", error=e)
            resolve_plans(cfg, catalog)
        try:
            if not cfg.plan_codes:
                raise ApiError("no plan codes yet (PLAN_MATCH matched nothing or catalog unavailable)")
            rows = check(cfg)
            watched = len({(r["sub"], r["fqn"]) for r in rows.values()})
            process(cfg, state, rows, catalog)
            state["last_ok"] = time.time()
            n_av = sum(r["available"] for r in rows.values())
            log("WARN" if not rows else "INFO", "check ok" if rows else "check ok but NOTHING matched filters, verify PLAN_CODES with --list",
                configs=watched, entries=len(rows), orderable=n_av)
            if fails and last_err_post:
                post_discord(cfg, content=f"✅ OVH API reachable again after {fmt_duration(now - first_fail)}.")
            fails, first_fail, last_err_post = 0, 0.0, 0.0
            delay = cfg.poll * random.uniform(0.8, 1.2)
        except ApiError as e:
            fails += 1
            first_fail = first_fail or now
            delay = min(900.0, 60.0 * 2 ** fails)
            log("WARN", "API check failed, backing off", error=e, failures=fails, retry_in=int(delay))
            if now - first_fail > 1800 and now - last_err_post > 3600:
                if post_discord(cfg, content=f"⚠️ OVH stock watcher: API failing for {fmt_duration(now - first_fail)}. Last error: `{e}`"):
                    last_err_post = now
        maybe_heartbeat(cfg, state, watched)
        save_state(cfg.state_path, state)
        time.sleep(delay)


# ---------------------------------------------------------------- CLI modes
def print_table(header: list[str], rows: list[list[str]]) -> None:
    w = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for r in [header, ["-" * n for n in w], *rows]:
        print("  ".join(str(x).ljust(n) for x, n in zip(r, w)).rstrip())


def cmd_list(cfg: Config, filt: str) -> int:
    rx = re.compile(filt, re.I) if filt else None
    for sub, base in cfg.endpoints:
        plans, addons = fetch_catalog(sub, base)
        out = []
        for it in fetch_availability(base):
            pc, desc = it.get("planCode") or "", describe(plans, it.get("planCode") or "")
            if pc not in plans or (rx and not rx.search(f"{pc} {desc} {it.get('memory')} {it.get('storage')}")):
                continue
            dcs = {str(d.get("datacenter")).lower(): d.get("availability") for d in it.get("datacenters") or []}
            out.append([pc, desc[:45], it.get("memory") or "", it.get("storage") or "",
                        disk_type(addons, it.get("storage") or "", pc), "yes" if config_ok(cfg, it) else "-",
                        *[dcs.get(dc, "") for dc in cfg.datacenters]])
        print(f"\n=== {sub} subsidiary ({base}) — {len(out)} eco configs" + (f" matching /{filt}/" if filt else "") + " ===")
        print_table(["PLAN CODE", "NAME / CPU", "MEMORY", "STORAGE", "DISK", "FILTER", *cfg.datacenters], sorted(out, key=lambda r: (r[1], r[0], r[2], r[3])))
    print("\nFILTER = passes STORAGE_INCLUDE/EXCLUDE and MEMORY_INCLUDE. Put the PLAN CODE into PLAN_CODES.")
    return 0


def cmd_once(cfg: Config) -> int:
    if cfg.plan_match and not cfg.plan_codes:
        resolve_plans(cfg, {sub: fetch_catalog(sub, base) for sub, base in cfg.endpoints})
    if not cfg.plan_codes:
        sys.exit("No plan codes: set PLAN_CODES (see --list) or a PLAN_MATCH that matches the catalog.")
    rows = sorted(check(cfg).values(), key=lambda r: (not r["available"], r["fqn"], r["dc"]))
    print_table(["SUB", "FQN", "DC", "AVAILABILITY", "ORDERABLE"],
                [[r["sub"], r["fqn"], r["dc"], r["availability"], "YES" if r["available"] else "no"] for r in rows])
    n = sum(r["available"] for r in rows)
    print(f"\n{len(rows)} watched entries, {n} orderable.")
    return 0 if n else 1


def cmd_test_alert(cfg: Config) -> int:
    if not (cfg.webhook or cfg.ntfy_topic):
        sys.exit("Set DISCORD_WEBHOOK_URL and/or NTFY_TOPIC first.")
    pc = cfg.plan_codes[0] if cfg.plan_codes else "TEST-PLAN"
    row = {"sub": cfg.endpoints[0][0], "plan": pc, "server": pc, "memory": "ram-128g-ecc-2400", "storage": "softraid-2x960nvme",
           "dc": cfg.datacenters[0] if cfg.datacenters else "bhs", "availability": "1H-low",
           "fqn": f"{pc}.ram-128g-ecc-2400.softraid-2x960nvme"}
    ok = notify_in_stock(cfg, row, "AMD EPYC 7351P (TEST)", label=f"[TEST] {cfg.label}")
    log("INFO" if ok else "ERROR", "test alert " + ("sent" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 in Docker ignores SIGTERM without a handler
    ap = argparse.ArgumentParser(description="Watch OVHcloud dedicated server availability and alert on Discord.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="single check, print matches, exit 0 if any orderable else 1")
    g.add_argument("--list", nargs="?", const="", metavar="REGEX", help="discovery: print eco catalog configs + availability")
    g.add_argument("--test-alert", action="store_true", help="send a fake in-stock alert")
    a, cfg = ap.parse_args(), Config()
    try:
        if a.list is not None:
            return cmd_list(cfg, a.list)
        if a.once:
            return cmd_once(cfg)
        if a.test_alert:
            return cmd_test_alert(cfg)
        run(cfg)
    except ApiError as e:
        log("ERROR", "API request failed", error=e)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
