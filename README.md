# ovh-stock-watch

A small daemon that polls OVHcloud's public dedicated-server availability API. It posts to a
Discord webhook (and, optionally, ntfy) when a server configuration you watch becomes orderable
in a datacenter you care about. The default target is a Kimsufi **KS-6 (AMD EPYC 7351P, 128 GB)
with SSD/NVMe storage** in **North America** (`bhs`, `hil`, `vin`).

Two ways to run it, with the same rules and messages:

- **Cloudflare Worker** (`src/index.ts`, recommended, free): a Cron Trigger checks every 3 minutes and Workers KV keeps the state.
- **Docker / Python** (`ovh_watch.py`): a single Python 3.12 file with no dependencies, for a home server. It also provides the `--list` discovery tool.
- Alerts only on a **transition** to available, then sends one "out of stock again" message with how long it lasted.
- State is kept in a small JSON file, so restarts don't re-alert.
- Exponential backoff on 429, 5xx and network errors (max 15 min). Posts to Discord only after 30 min of failures, at most once per hour.
- Sends a daily heartbeat so you know it's still alive.

## Cloudflare Worker (free)

1. **Find the plan code.** Run the `discover` workflow from the repo's **Actions** tab. It also runs automatically when `ovh_watch.py` changes. It runs `--list` against the real OVH API (US + CA) and commits the result to `DISCOVERY.md`. For the KS-6 this is already set: `24sk602-v1-us` covers hil/vin and `24sk602-v1-ca` covers bhs, both on the US API.
2. **Connect the repo.** In the Cloudflare dashboard, go to **Workers & Pages → Create → Import a repository** and pick this repo. Keep the defaults (deploy command `npx wrangler deploy`). Every push to `main` redeploys.
3. **Add the secrets.** Open the Worker, go to **Settings → Variables and Secrets**, and add `DISCORD_WEBHOOK_URL` as a **Secret**. Optionally also add `DISCORD_MENTION` (e.g. `<@&roleid>`), `ORDER_URL`, `NTFY_TOPIC` and `NTFY_TOKEN`.
4. **Wait for the first run.** Within 3 minutes Discord should get *"✅ OVH stock watcher deployed on Cloudflare…"*. That confirms the webhook works.

Notes:

- The KV namespace `ovh-watcher-state` is already referenced in `wrangler.jsonc`.
- Logs are under **Workers → ovh-watcher → Logs**.
- The Worker's URL returns a read-only status JSON: last OK check, configs watched, and what's in stock.
- Usage per day: ~480 runs, a couple of OVH requests per run, and one KV write per run. That's well inside the Workers free plan (100k requests/day, 1,000 KV writes/day).
- The schedule is fixed (`*/3 * * * *`), with no jitter. To change it, edit `triggers.crons`.
- Non-secret settings live in `vars` in `wrangler.jsonc`. `keep_vars` is on, so anything you add in the dashboard survives deploys.

## Docker / home server

### 1. Setup

```bash
cp .env.example .env
```

Edit `.env` and set at least `DISCORD_WEBHOOK_URL`. You'll fill in `PLAN_CODES` in the next step.
To create the webhook in Discord: **Server Settings → Integrations → Webhooks → New Webhook → Copy URL**.

### 2. Find the plan code with `--list`

Discovery mode fetches the eco catalog (`/order/catalog/public/eco`) and the availability
endpoint. It prints every eco config with its per-datacenter availability:

```bash
docker compose run --rm ovh-stock-watch --list ks-6
```

Without Docker:

```bash
python3 ovh_watch.py --list ks-6
```

The optional argument is a case-insensitive regex matched against the plan code, name/CPU,
memory and storage. Omit it to list everything.

```
=== US subsidiary (https://api.us.ovhcloud.com/1.0) — 3 eco configs matching /ks-6/ ===
PLAN CODE  NAME / CPU             MEMORY             STORAGE             DISK  FILTER  bhs          hil  vin
---------  ---------------------  -----------------  ------------------  ----  ------  -----------  ---  -------
25sk60     KS-6 | AMD EPYC 7351P  ram-128g-ecc-2400  softraid-2x4000sa   SATA  -       1H-high      24H
25sk60     KS-6 | AMD EPYC 7351P  ram-128g-ecc-2400  softraid-2x512nvme  NVMe  yes     unavailable
```

*(The example output above uses made-up codes. Use the real ones from your own output.)*

- **PLAN CODE**: put this into `PLAN_CODES`.
- **DISK**: the disk technology from the catalog (NVMe, SSD, SATA, ...). Use it to confirm which storage codes are flash.
- **FILTER**: `yes` if the row passes your current `STORAGE_INCLUDE`, `STORAGE_EXCLUDE` and `MEMORY_INCLUDE`. These are the rows that will alert.
- **bhs / hil / vin**: the current availability. Blank means the config isn't offered in that datacenter.

Stock can differ between the US and Canadian subsidiaries. Kimsufi in Beauharnois is often sold
through the CA subsidiary. Set `OVH_ENDPOINTS=us,ca` to list and watch both.

> **Hybrid storage:** a code like `hybridsoftraid-2x4000sa-1x512nvme` contains `nvme`, so it passes
> the default filter. If you want pure flash only, set `STORAGE_EXCLUDE=hdd|sata|hybrid`.

### 3. Check once

```bash
docker compose run --rm ovh-stock-watch --once
```

This runs a single check of your configured filters and prints the matching entries. It does not
send alerts or touch the state file.

| Exit code | Meaning |
|---|---|
| `0` | At least one match is orderable |
| `1` | Nothing orderable |
| `2` | API error |

### 4. Test the webhook

```bash
docker compose run --rm ovh-stock-watch --test-alert
```

This sends a fake `[TEST]` in-stock embed, including your `DISCORD_MENTION` ping, the order link
and the ntfy push. Use it to confirm your phone actually buzzes.

### 5. Run it

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

State lives in the named volume `ovh-stock-watch-data` at `/data/state.json`. If you'd rather use
a bind mount, make sure the directory is writable by UID 10001.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `PLAN_CODES` | | Comma-separated plan codes from `--list` (preferred) |
| `PLAN_MATCH` | | Alternative to `PLAN_CODES`: regex on the catalog name/CPU, e.g. `KS-6(?![\w-]).*7351P`. Resolved at startup and daily; resolved codes are logged |
| `DATACENTERS` | `bhs,hil,vin` | Datacenter codes to watch |
| `OVH_ENDPOINTS` | `us` | `us`, `ca`, `us,ca`, or a custom `SUB=https://host/1.0` |
| `STORAGE_INCLUDE` | `nvme\|ssd` | Regex the storage code must match |
| `STORAGE_EXCLUDE` | `hdd\|sata` | Regex the storage code must NOT match |
| `MEMORY_INCLUDE` | *(empty)* | Optional regex on the memory code, e.g. `128g` |
| `POLL_SECONDS` | `180` | ±20% random jitter; minimum 30 |
| `DISCORD_WEBHOOK_URL` | | Discord webhook |
| `DISCORD_MENTION` | | `<@&ROLE_ID>` or `<@USER_ID>`, pinged on in-stock alerts only |
| `ORDER_URL` | | Linked from alerts (and the ntfy click action) |
| `ALERT_LABEL` | `KS-6 NVMe` | Used in titles: `🟢 KS-6 NVMe in stock — Beauharnois, QC` |
| `NTFY_TOPIC` / `NTFY_SERVER` / `NTFY_TOKEN` | — / `https://ntfy.sh` / — | Optional ntfy push (priority high) |
| `HEARTBEAT_TIME` | `09:00` | Local time (set `TZ`); `off` to disable |
| `STATE_PATH` | `./state.json` (`/data/state.json` in Docker) | |

**What counts as orderable:** any `availability` value other than `unavailable` or `comingSoon`
(for example `1H-low`, `1H-high`, `24H` or `72H`).

**When an alert is sent:** an alert is sent per (subsidiary, fqn, datacenter) when the config
goes from not orderable (or unseen) to orderable. If delivery fails on every notifier, the state
isn't updated, so the next poll tries again.

## Logs

Logs are timestamped logfmt, one event per line:

```
ts=2026-11-11T01:15:20+00:00 level=INFO msg="IN STOCK" key=US|25sk60.ram-128g-ecc-2400.softraid-2x512nvme|bhs availability=1H-low
ts=2026-11-11T01:20:20+00:00 level=WARN msg="API check failed, backing off" error="HTTP 503 ..." failures=1 retry_in=120
```

If every check logs `NOTHING matched filters`, your `PLAN_CODES` or regexes don't match anything
the API returns. Re-run `--list`. The daily heartbeat also shows `0 configs watched` in that case.
