# Deploying QF-gate on a VPS (no Docker)

Target: one small Ubuntu 24.04 VPS (1 vCPU / 2 GB RAM is enough for a few coins), PostgreSQL,
four systemd services and Caddy for HTTPS. Everything below runs as a dedicated `qfgate` user.

```
web       gunicorn  -> 127.0.0.1:8000  (Caddy in front, HTTPS)
worker    manage.py run_worker      (jobs, schedule, bot safety-net steps, alerts)
stream    manage.py stream_candles  (live 1m candles, bot stops between bars)
telegram  manage.py telegram_bot    (optional: Telegram commands)
```

## 1. Server basics

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv python3-dev build-essential postgresql caddy git ufw
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
sudo adduser --system --group --home /srv/qfgate qfgate
```

## 2. Database

```bash
sudo -u postgres psql -c "CREATE USER qfgate WITH PASSWORD 'choose-a-long-password';"
sudo -u postgres psql -c "CREATE DATABASE qfgate OWNER qfgate;"
```

## 3. Code and settings

```bash
sudo -u qfgate git clone <your repo url> /srv/qfgate/app      # or copy the project folder
cd /srv/qfgate/app
sudo -u qfgate python3 -m venv /srv/qfgate/venv
sudo -u qfgate /srv/qfgate/venv/bin/pip install -r requirements.txt
sudo -u qfgate cp .env.example .env && sudo -u qfgate nano .env
```

In `.env` set at least:

```
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<50+ random characters>
DJANGO_ALLOWED_HOSTS=your.domain
DJANGO_CSRF_TRUSTED_ORIGINS=https://your.domain
POSTGRES_DB=qfgate
POSTGRES_USER=qfgate
POSTGRES_PASSWORD=<the password from step 2>
MARKET_HISTORY_DAYS=365
LIVE_TRADING=false
```

Keep `LIVE_TRADING=false` until you deliberately go live. Run `chmod 600 .env` (it holds secrets). Then:

```bash
sudo -u qfgate /srv/qfgate/venv/bin/python manage.py migrate
sudo -u qfgate /srv/qfgate/venv/bin/python manage.py collectstatic --noinput
sudo -u qfgate /srv/qfgate/venv/bin/python manage.py createsuperuser
```

Moving your local SQLite data: on your PC run
`python manage.py dumpdata market trading auth --natural-foreign --natural-primary -o backup.json`,
copy `backup.json` to the server and run `manage.py loaddata backup.json` after `migrate`.
Or skip it and let the server sync 365 days itself (about 15 minutes per coin).

## 4. Services

```bash
sudo cp deploy/systemd/qfgate-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qfgate-web qfgate-worker qfgate-stream
sudo systemctl enable --now qfgate-telegram     # only after TELEGRAM_* are set in .env
systemctl status 'qfgate-*'
journalctl -u qfgate-stream -f                  # live logs
```

All services restart automatically if they crash and start at boot.

## 5. HTTPS

Point your domain's DNS A record at the server, edit the domain in `deploy/Caddyfile`, then:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Caddy obtains and renews the TLS certificate automatically.

## 6. Backups

```bash
sudo cp deploy/backup.sh /srv/qfgate/backup.sh && sudo chmod 750 /srv/qfgate/backup.sh
sudo crontab -u qfgate -e      # add:  15 3 * * * /srv/qfgate/backup.sh
```

Keeps 14 daily compressed dumps in `/srv/qfgate/backups`. Copy them off the server regularly
(for example with `rclone` to cloud storage). **Test a restore** before you rely on it:

```bash
createdb -U qfgate qfgate_restore_test
gunzip -c /srv/qfgate/backups/<file>.sql.gz | psql -U qfgate qfgate_restore_test
```

## 7. Updating

```bash
cd /srv/qfgate/app && sudo -u qfgate git pull
sudo -u qfgate /srv/qfgate/venv/bin/pip install -r requirements.txt
sudo -u qfgate /srv/qfgate/venv/bin/python manage.py migrate
sudo -u qfgate /srv/qfgate/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart qfgate-web qfgate-worker qfgate-stream qfgate-telegram
```

## Going live (later, deliberately)

1. Run a dry-run bot for at least the configured number of days (Control page) and compare its
   actual fills with the model P&L on the bot page.
2. Create a Gate.io API key with **spot trade permission only**, **IP-whitelisted to this server**,
   **no withdrawals**. Put it in `.env` as `GATE_API_KEY` / `GATE_API_SECRET`, set `LIVE_TRADING=true`,
   and restart the services.
3. On the Control page set the risk limits, then switch live trading on (type LIVE).
4. Start with the smallest position size Gate allows and watch the first trades.
