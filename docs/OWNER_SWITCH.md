# Owner switch

The one door through which the owner's phone records **Stop**, **Freeze** or
**Start** (owner ruling 2026-10-09). The desk already obeys those intents
(`src/owner_intents.py`, `src/owner_flags.py`); this service only records one
of them, through the existing writer. It touches no broker and runs no desk
work. The dashboard API (`src/api/`) stays read-only and is unchanged.

- Stop: desk fully off; positions and broker-held stops kept.
- Freeze: no new buys; exits keep working.
- Start: clears Stop and Freeze.

A recorded intent takes effect at the desk's next pickup (desk start, before
every scheduled job, and in every unit's Stop gate).

## How a press is checked

The service listens on `127.0.0.1:8801` only; the phone reaches it through
Tailscale Serve. `GET /` serves a one-page form; `POST /action` with JSON
`{"action": "stop" | "freeze" | "start"}` records the intent. A POST is
accepted only when all of these hold:

1. `Origin` equals `OWNER_SWITCH_ORIGIN` (and a `Referer`, if sent, is on it).
   A missing or foreign Origin is refused: this is the CSRF guard. No cookies.
2. `Tailscale-User-Login` equals `OWNER_SWITCH_OWNER_LOGIN`. Tailscale Serve
   sets this header for the signed-in tailnet user.
3. `X-Owner-Token` matches the secret in the token file. This second factor
   matters because any local process could forge the identity header on loopback.
4. The action is one of the three words; anything else is 400.

Refusals are 403 (checks 1-3) or 400 (check 4). Other methods are 405. Every
decision is logged to the journal with time, action and reason; token values
are never logged. The page keeps the token in memory only (no cookie, no
storage); a reload forgets it, and a 403 drops it.

The service refuses to start if the login or origin is unset, the token file
is missing, readable by group or others, or shorter than 32 characters.

## Configuration (never in the repo; the repo is public)

| Setting | Where |
|---|---|
| `OWNER_SWITCH_OWNER_LOGIN` | `.env` — the owner's tailnet login, e.g. as shown by `tailscale status` |
| `OWNER_SWITCH_ORIGIN` | `.env` — the exact `https://` address the phone opens, with port if not 443 |
| token | `/home/qamc/credentials/owner_switch_token`, mode `0600`, owner `qamc`; the default path; `OWNER_SWITCH_TOKEN_FILE` overrides it (tests) |
| `OWNER_SWITCH_DB_PATH` | optional; defaults to `storage.db_path` in `config/settings.yaml` |
| `OWNER_SWITCH_PORT` | set to `8801` in the unit |

The unit `scripts/systemd/quant-agent-owner-switch.service` has no Stop gate
on purpose (Start is pressed through it); `tests/test_owner_stop_timers.py`
names it as an exemption. A missing or loose token file keeps it down (exit 2, not restarted).

## Deploying (not done by the change that added it)

1. Create the token: `install -m 0600 /dev/null /home/qamc/credentials/owner_switch_token && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /home/qamc/credentials/owner_switch_token`. Put the same value in the phone's password manager.
2. Add `OWNER_SWITCH_OWNER_LOGIN=` and `OWNER_SWITCH_ORIGIN=` to `/home/qamc/quant-agent/.env`.
3. Install and start the unit: `cp scripts/systemd/quant-agent-owner-switch.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now quant-agent-owner-switch.service`.
4. Publish it to the tailnet only (tailnet, never Funnel), on its own HTTPS port:
   `tailscale serve --bg --https=8443 http://127.0.0.1:8801`
   Then `OWNER_SWITCH_ORIGIN` is `https://<machine>.<tailnet>.ts.net:8443`.
5. Check: `journalctl --user -u quant-agent-owner-switch -n 20` shows `listening`; a press from the phone shows `ACCEPTED`.
