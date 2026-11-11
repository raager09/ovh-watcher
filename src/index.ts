// OVHcloud dedicated-server stock watcher as a Cloudflare Worker (Cron Trigger + KV state).
// Same rules as ovh_watch.py: alert on unavailable -> orderable, "out of stock again" with duration,
// exponential backoff on API errors, error post after 30 min (max 1/h), daily heartbeat.

export interface Env {
  STATE: KVNamespace;
  PLAN_CODES?: string;
  DATACENTERS?: string;
  OVH_ENDPOINTS?: string;
  STORAGE_INCLUDE?: string;
  STORAGE_EXCLUDE?: string;
  MEMORY_INCLUDE?: string;
  DISCORD_WEBHOOK_URL?: string;
  DISCORD_MENTION?: string;
  ORDER_URL?: string;
  ALERT_LABEL?: string;
  NTFY_TOPIC?: string;
  NTFY_SERVER?: string;
  NTFY_TOKEN?: string;
  HEARTBEAT_TIME?: string;
  TZ?: string;
}

type Row = { sub: string; fqn: string; plan: string; server: string; memory: string; storage: string; dc: string; availability: string; available: boolean };
type Item = { available: boolean; since: number; row?: Row };
type State = {
  items: Record<string, Item>; lastOk?: number; watched?: number; fails?: number; firstFail?: number;
  nextAttempt?: number; lastErrPost?: number; lastHeartbeat?: string; announced?: boolean;
  lastRun?: number; lastDiscordError?: string; lastDiscordOk?: number;
};

const ENDPOINTS: Record<string, [string, string]> = { us: ["US", "https://api.us.ovhcloud.com/1.0"], ca: ["CA", "https://ca.api.ovh.com/1.0"] };
const DC_NAMES: Record<string, string> = { bhs: "Beauharnois, QC", hil: "Hillsboro, OR", vin: "Vint Hill, VA", tor: "Toronto",
  gra: "Gravelines", rbx: "Roubaix", sbg: "Strasbourg", lon: "London", fra: "Frankfurt", waw: "Warsaw" };
const NOT_ORDERABLE = new Set(["unavailable", "comingsoon"]);
const UA = "ovh-stock-watch/1.0 (cloudflare-worker)";

const log = (level: string, msg: string, kv: Record<string, unknown> = {}) => console.log(JSON.stringify({ level, msg, ...kv }));
const csv = (s?: string) => (s ?? "").split(",").map((x) => x.trim()).filter(Boolean);
const dcName = (dc: string) => DC_NAMES[dc] ?? dc.toUpperCase();
const dcLabel = (dc: string) => `${dcName(dc)} (${dc})`;

class ApiError extends Error {}
let discordOutcome: { ok?: number; error?: string } = {};

function config(env: Env) {
  const endpoints = csv(env.OVH_ENDPOINTS || "us").map((e): [string, string] => {
    if (ENDPOINTS[e.toLowerCase()]) return ENDPOINTS[e.toLowerCase()];
    const i = e.indexOf("=");
    if (i > 0) return [e.slice(0, i).toUpperCase(), e.slice(i + 1).replace(/\/$/, "")];
    throw new Error(`Unknown OVH_ENDPOINTS entry ${e}`);
  });
  return {
    env, endpoints,
    planCodes: csv(env.PLAN_CODES),
    datacenters: csv(env.DATACENTERS || "bhs,hil,vin").map((d) => d.toLowerCase()),
    storageInc: new RegExp(env.STORAGE_INCLUDE || "nvme|ssd", "i"),
    storageExc: new RegExp(env.STORAGE_EXCLUDE || "hdd|sata", "i"),
    memoryInc: env.MEMORY_INCLUDE ? new RegExp(env.MEMORY_INCLUDE, "i") : null,
    label: env.ALERT_LABEL || "KS-6 NVMe",
    heartbeat: /^(off|none|disabled)$/i.test(env.HEARTBEAT_TIME ?? "") ? "" : env.HEARTBEAT_TIME || "09:00",
    tz: env.TZ || "UTC",
  };
}
type Cfg = ReturnType<typeof config>;

async function getJson(url: string): Promise<any> {
  let r: Response;
  try {
    r = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json" } });
  } catch (e) {
    throw new ApiError(`network error: ${e} (${url})`);
  }
  if (!r.ok) throw new ApiError(`HTTP ${r.status} from ${url}`);
  try {
    return await r.json();
  } catch (e) {
    throw new ApiError(`bad JSON from ${url}: ${e}`);
  }
}

const isOrderable = (av: string) => !!av && !NOT_ORDERABLE.has(av.toLowerCase());

async function check(cfg: Cfg): Promise<Record<string, Row>> {
  const rows: Record<string, Row> = {};
  for (const [sub, base] of cfg.endpoints) {
    for (const pc of cfg.planCodes) {
      const items: any[] = (await getJson(`${base}/dedicated/server/datacenter/availabilities?planCode=${encodeURIComponent(pc)}`)) ?? [];
      for (const it of items) {
        const st = String(it.storage ?? ""), mem = String(it.memory ?? "");
        if (it.planCode !== pc || !cfg.storageInc.test(st) || cfg.storageExc.test(st) || (cfg.memoryInc && !cfg.memoryInc.test(mem))) continue;
        const fqn = it.fqn ?? `${pc}.${mem}.${st}`;
        for (const d of it.datacenters ?? []) {
          const dc = String(d.datacenter ?? "").toLowerCase();
          if (!cfg.datacenters.includes(dc)) continue;
          const av = d.availability || "unavailable";
          rows[`${sub}|${fqn}|${dc}`] = { sub, fqn, plan: pc, server: it.server ?? pc, memory: mem, storage: st, dc, availability: av, available: isOrderable(av) };
        }
      }
    }
  }
  return rows;
}

// ------------------------------------------------------------------ notifiers
async function postDiscord(cfg: Cfg, body: { embed?: object; content?: string; mention?: boolean }): Promise<boolean> {
  const url = (cfg.env.DISCORD_WEBHOOK_URL ?? "").trim();
  if (!url) return false;
  const text = [body.mention ? cfg.env.DISCORD_MENTION : "", body.content].filter(Boolean).join(" ");
  const payload: Record<string, unknown> = { embeds: body.embed ? [body.embed] : [] };
  if (text) payload.content = text;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(url + (url.includes("?") ? "&" : "?") + "wait=true", {
        method: "POST", headers: { "Content-Type": "application/json", "User-Agent": UA }, body: JSON.stringify(payload) });
      if (r.ok) {
        discordOutcome.ok = Date.now();
        return true;
      }
      if (r.status === 429 && attempt === 0) {
        await new Promise((res) => setTimeout(res, Math.min(5000, 1000 * Number(r.headers.get("Retry-After") ?? 2))));
        continue;
      }
      const body = (await r.text()).replace(/\s+/g, " ").slice(0, 300);
      discordOutcome.error = `${new Date().toISOString()} HTTP ${r.status}: ${body}`;
      log("ERROR", "discord post failed", { status: r.status, body });
      return false;
    } catch (e) {
      discordOutcome.error = `${new Date().toISOString()} ${String(e)}`;
      log("ERROR", "discord post failed", { error: String(e) });
      return false;
    }
  }
  return false;
}

async function postNtfy(cfg: Cfg, title: string, message: string, priority: number, tags: string[]): Promise<boolean> {
  if (!cfg.env.NTFY_TOPIC) return false;
  const body: Record<string, unknown> = { topic: cfg.env.NTFY_TOPIC, title, message, priority, tags };
  if (cfg.env.ORDER_URL) body.click = cfg.env.ORDER_URL;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (cfg.env.NTFY_TOKEN) headers.Authorization = `Bearer ${cfg.env.NTFY_TOKEN}`;
  try {
    const r = await fetch((cfg.env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, ""), { method: "POST", headers, body: JSON.stringify(body) });
    if (!r.ok) log("ERROR", "ntfy post failed", { status: r.status });
    return r.ok;
  } catch (e) {
    log("ERROR", "ntfy post failed", { error: String(e) });
    return false;
  }
}

function fmtDuration(ms: number): string {
  let m = Math.floor(ms / 60000);
  const d = Math.floor(m / 1440); m -= d * 1440;
  const h = Math.floor(m / 60); m -= h * 60;
  return [[d, "d"], [h, "h"], [m, "m"]].filter(([v]) => v).map(([v, u]) => `${v}${u}`).join(" ") || "<1m";
}

export async function notifyInStock(cfg: Cfg, row: Row, label = cfg.label): Promise<boolean> {
  const now = Math.floor(Date.now() / 1000);
  const title = `🟢 ${label} in stock — ${dcName(row.dc)}`;
  const embed: Record<string, unknown> = {
    title, color: 0x2ecc71, timestamp: new Date().toISOString(),
    fields: [
      { name: "Plan code", value: `\`${row.plan}\``, inline: true },
      { name: "CPU / RAM", value: `${row.server}\n\`${row.memory}\``, inline: true },
      { name: "Storage", value: `\`${row.storage}\``, inline: true },
      { name: "Datacenter", value: dcLabel(row.dc), inline: true },
      { name: "Availability", value: `\`${row.availability}\``, inline: true },
      { name: "Detected", value: `<t:${now}:F> (<t:${now}:R>)`, inline: true },
    ],
    footer: { text: `${row.fqn} · ${row.sub} API` },
  };
  if (cfg.env.ORDER_URL) {
    embed.url = cfg.env.ORDER_URL;
    embed.description = `**[Order now →](${cfg.env.ORDER_URL})**`;
  }
  const d = await postDiscord(cfg, { embed, mention: true });
  const n = await postNtfy(cfg, title, `${row.fqn} @ ${dcLabel(row.dc)}, delay ${row.availability}`, 4, ["green_circle"]);
  return d || n;
}

async function notifyOutOfStock(cfg: Cfg, row: Row, lasted: number): Promise<void> {
  const title = `🔴 ${cfg.label} out of stock again — ${dcName(row.dc)}`;
  const embed = {
    title, color: 0xe74c3c, timestamp: new Date().toISOString(),
    fields: [
      { name: "Plan code", value: `\`${row.plan}\``, inline: true },
      { name: "Storage", value: `\`${row.storage}\``, inline: true },
      { name: "Datacenter", value: dcLabel(row.dc), inline: true },
      { name: "Was in stock for", value: fmtDuration(lasted), inline: true },
    ],
    footer: { text: `${row.fqn} · ${row.sub} API` },
  };
  await postDiscord(cfg, { embed });
  await postNtfy(cfg, title, `${row.fqn} gone after ${fmtDuration(lasted)}`, 3, ["red_circle"]);
}

// ------------------------------------------------------------------ state machine
async function process(cfg: Cfg, state: State, rows: Record<string, Row>): Promise<void> {
  const items = state.items, now = Date.now();
  for (const [key, row] of Object.entries(rows)) {
    const prev = items[key];
    if (row.available && !prev?.available) {
      log("INFO", "IN STOCK", { key, availability: row.availability });
      if (await notifyInStock(cfg, row)) items[key] = { available: true, since: now, row };
      else log("ERROR", "alert not delivered, will retry next run", { key });
    } else if (row.available && prev) {
      prev.row = row;
    } else if (prev?.available) {
      log("INFO", "out of stock again", { key, lasted: fmtDuration(now - prev.since) });
      await notifyOutOfStock(cfg, row, now - prev.since);
      items[key] = { available: false, since: now };
    } else if (!prev) {
      items[key] = { available: false, since: now };
    }
  }
  for (const key of Object.keys(items)) {
    if (key in rows) continue; // config vanished from the API response = gone
    const prev = items[key];
    delete items[key];
    const r = prev.row;
    if (prev.available && r && cfg.planCodes.includes(r.plan) && cfg.datacenters.includes(r.dc)) await notifyOutOfStock(cfg, r, now - prev.since);
  }
}

function localNow(tz: string): { date: string; hm: string } {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date()).map((x) => [x.type, x.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, hm: `${p.hour}:${p.minute}` };
}

async function maybeHeartbeat(cfg: Cfg, state: State): Promise<void> {
  if (!cfg.heartbeat) return;
  const { date, hm } = localNow(cfg.tz);
  if (state.lastHeartbeat === date || hm < cfg.heartbeat.padStart(5, "0")) return;
  const ok = state.lastOk ? `last check OK <t:${Math.floor(state.lastOk / 1000)}:R>` : "NO successful check yet";
  const msg = `💓 Stock watcher alive, ${ok}, ${state.watched ?? 0} configs watched (${cfg.planCodes.join(", ") || "no PLAN_CODES set"} @ ${cfg.datacenters.join(", ")})`;
  const sent = cfg.env.DISCORD_WEBHOOK_URL ? await postDiscord(cfg, { content: msg }) : await postNtfy(cfg, "Stock watcher alive", msg, 2, ["heartbeat"]);
  if (sent) state.lastHeartbeat = date;
}

export async function runOnce(env: Env): Promise<State> {
  const cfg = config(env);
  discordOutcome = {};
  const state: State = (await env.STATE.get<State>("state", "json")) ?? { items: {} };
  state.items ??= {};
  const now = Date.now();
  if (!state.announced && cfg.env.DISCORD_WEBHOOK_URL?.trim()) {  // only marked once Discord actually accepted it
    state.announced = await postDiscord(cfg, { content: `✅ OVH stock watcher deployed on Cloudflare: watching ${cfg.planCodes.join(", ") || "⚠️ no PLAN_CODES set"} @ ${cfg.datacenters.join(", ")} (${cfg.endpoints.map((e) => e[0]).join("+")} API).` });
  }
  if (state.nextAttempt && now < state.nextAttempt) {
    log("INFO", "backing off, skipping this run", { retryAt: new Date(state.nextAttempt).toISOString() });
  } else {
    try {
      if (!cfg.planCodes.length) throw new ApiError("PLAN_CODES is empty");
      const rows = await check(cfg);
      state.watched = new Set(Object.values(rows).map((r) => `${r.sub}|${r.fqn}`)).size;
      await process(cfg, state, rows);
      const orderable = Object.values(rows).filter((r) => r.available).length;
      log(Object.keys(rows).length ? "INFO" : "WARN", Object.keys(rows).length ? "check ok" : "check ok but NOTHING matched filters",
        { configs: state.watched, entries: Object.keys(rows).length, orderable });
      if (state.fails && state.lastErrPost) await postDiscord(cfg, { content: `✅ OVH API reachable again after ${fmtDuration(now - (state.firstFail ?? now))}.` });
      state.lastOk = now;
      state.fails = 0; state.firstFail = 0; state.lastErrPost = 0; state.nextAttempt = 0;
    } catch (e) {
      if (!(e instanceof ApiError)) log("ERROR", "unexpected error", { error: String((e as Error)?.stack ?? e) });
      state.fails = (state.fails ?? 0) + 1;
      state.firstFail ||= now;
      const delay = Math.min(900, 60 * 2 ** state.fails) * 1000;
      state.nextAttempt = now + delay;
      log("WARN", "API check failed, backing off", { error: String((e as Error)?.message ?? e), failures: state.fails, retryInSec: delay / 1000 });
      if (now - state.firstFail > 30 * 60000 && now - (state.lastErrPost ?? 0) > 60 * 60000) {
        if (await postDiscord(cfg, { content: `⚠️ OVH stock watcher: API failing for ${fmtDuration(now - state.firstFail)}. Last error: \`${String((e as Error)?.message ?? e).slice(0, 300)}\`` })) state.lastErrPost = now;
      }
    }
  }
  await maybeHeartbeat(cfg, state);
  state.lastRun = now;
  if (discordOutcome.ok) state.lastDiscordOk = discordOutcome.ok;
  if (discordOutcome.error) state.lastDiscordError = discordOutcome.error;
  await env.STATE.put("state", JSON.stringify(state));
  return state;
}

export default {
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runOnce(env));
  },
  // GET / -> status JSON (read-only; no way to trigger alerts over HTTP).
  async fetch(_req: Request, env: Env): Promise<Response> {
    const s = (await env.STATE.get<State>("state", "json")) ?? { items: {} };
    const inStock = Object.entries(s.items ?? {}).filter(([, v]) => v.available).map(([k, v]) => ({ key: k, since: new Date(v.since).toISOString(), availability: v.row?.availability }));
    return Response.json({
      plans: csv(env.PLAN_CODES), datacenters: csv(env.DATACENTERS || "bhs,hil,vin"),
      lastOk: s.lastOk ? new Date(s.lastOk).toISOString() : null, configsWatched: s.watched ?? 0,
      failures: s.fails ?? 0, inStock,
      lastRun: s.lastRun ? new Date(s.lastRun).toISOString() : null,
      webhookConfigured: !!env.DISCORD_WEBHOOK_URL?.trim(),
      deployMessageSent: !!s.announced,
      lastDiscordOk: s.lastDiscordOk ? new Date(s.lastDiscordOk).toISOString() : null,
      lastDiscordError: s.lastDiscordError ?? null,
    });
  },
} satisfies ExportedHandler<Env>;
