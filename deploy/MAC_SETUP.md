# Clawdbot on a Mac mini — setup (Phase 7)

One-time setup for running Clawdbot 24/7 as a per-user LaunchAgent. Do the steps in order; the
permission grants need you at the Mac's display. Afterwards the bot survives reboots, restarts
itself after crashes or `/update`, and `/doctor` in Telegram tells you if anything is off.

Throughout, `__PROJECT__` means the absolute project path (e.g. `/Users/josh/Clawdbot`) and
`__USER__` your macOS short username (`whoami`).

## 1. macOS prerequisites (at the display)

1. **Account:** use a normal user account that is logged in at boot. System Settings → Users &
   Groups → Login Options → Automatic login: on. (FileVault blocks automatic login; if you want
   FileVault, you'll log in once after each reboot.)
2. **Never sleep:** System Settings → Energy → "Prevent automatic sleeping when the display is off"
   on, "Start up automatically after a power failure" on. Or from Terminal:
   `sudo pmset -a sleep 0 disksleep 0 autorestart 1`.
3. **Messages:** open Messages, sign in with your Apple ID. On the iPhone: Settings → Messages →
   Text Message Forwarding → enable the Mac (so SMS arrives, not just iMessage).
4. **WhatsApp (read-only source):** install WhatsApp from the Mac App Store, link it with your
   phone (QR), let it sync history.
5. **Calendar:** open Calendar once so your iCloud calendars sync (needed for Phase 10).
6. **Xcode command line tools** (for git): `xcode-select --install`.

## 2. Python and the project

```bash
# Python 3.14 from python.org (or: brew install python@3.14)
cd ~
git clone <repo-url> Clawdbot
cd Clawdbot
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

`requirements.lock` is the exact set verified on the dev box; fall back to `requirements.txt` if
a pin has no macOS wheel.

## 3. Copy the live data from the Windows box

These are gitignored and must be copied by hand (AirDrop, `scp`, or a USB stick) into the
project root:

- `.env`
- `credentials.json` (Google OAuth client)
- `token_personal.json`, `token_work.json` (optional — you can re-auth instead, step 5)
- `clawdbot.db` (the index, memories, audit log — skip for a clean start)

Then lock them down: `chmod 600 .env credentials.json token_*.json`.

Make sure the project folder is **not** inside iCloud Drive / Desktop / Documents if iCloud
"Desktop & Documents" sync is on — a synced SQLite file corrupts.

## 4. First manual run

```bash
cd ~/Clawdbot
.venv/bin/python -X utf8 main.py
```

You should get a "🟢 Clawdbot started" message in Telegram. Send `/doctor`. Expect ❌ on the
Mac data sources until step 6; fix anything else it reports first. Ctrl-C to stop.

## 5. Gmail tokens that don't expire

In Google Cloud Console → APIs & Services → OAuth consent screen, set **Publishing status** to
**In production** (unverified is fine for a personal app; Google shows a one-time warning on
sign-in). Apps left in *Testing* get refresh tokens that die after 7 days — that was the cause
of the `invalid_grant` floods on Windows.

Then, with the bot running, send `/addaccount personal` and `/addaccount work` in Telegram; the
browser opens on the Mac. `/doctor` should show each Gmail account as ✅.

## 6. Permissions (TCC) — at the display

The grants must go to the **real** interpreter, not the venv symlink. Find it:

```bash
readlink -f ~/Clawdbot/.venv/bin/python
# e.g. /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
```

System Settings → Privacy & Security:

- **Full Disk Access** → `+` → press ⌘⇧G and paste that path → add, toggle on.
  (Reads `~/Library/Messages/chat.db`, WhatsApp's `ChatStorage.sqlite`, Contacts.)
- **Automation** → when the bot first sends an iMessage (Phase 8) macOS prompts "python wants
  to control Messages" → Allow. If you missed it: Automation → python3.14 → Messages on.
- **Calendars** (Phase 10) → same, prompt on first use → Allow.

Restart the bot after changing permissions; `/doctor` should now show iMessage, WhatsApp and
Contacts as ✅.

## 7. Install the LaunchAgent

```bash
mkdir -p ~/Library/Logs/clawdbot
sed -e "s#__PROJECT__#$HOME/Clawdbot#g" -e "s#__USER__#$(whoami)#g" \
    ~/Clawdbot/deploy/com.clawdbot.bot.plist > ~/Library/LaunchAgents/com.clawdbot.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clawdbot.bot.plist
launchctl kickstart -k gui/$(id -u)/com.clawdbot.bot
```

Check: `launchctl print gui/$(id -u)/com.clawdbot.bot | grep -E "state|pid"` and the Telegram
"started" ping. Logs: `~/Library/Logs/clawdbot/` (process stdout/stderr) and
`~/Clawdbot/clawdbot.log` (the app log, rotates at 10 MB × 5).

Useful later:

```bash
launchctl kickstart -k gui/$(id -u)/com.clawdbot.bot   # restart now
launchctl bootout gui/$(id -u)/com.clawdbot.bot         # stop and unload
```

From Telegram, `/restart` and `/update` do the same without SSH: the bot exits with code 0 and
`KeepAlive` brings it back.

## 8. Backups

The bot writes an online SQLite backup of `clawdbot.db` every night at `BACKUP_HOUR` (default
03:00) into `backups/` (or `BACKUP_DIR`), keeping the newest `BACKUP_KEEP` (14). Point Time
Machine at the Mac too; the `backups/` folder is what you restore from if the DB corrupts:

```bash
launchctl bootout gui/$(id -u)/com.clawdbot.bot
cp backups/clawdbot-YYYYMMDD-HHMMSS.db clawdbot.db
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clawdbot.bot.plist
```

## 9. Reboot test

Reboot the Mac. Without touching it you should get the "🟢 Clawdbot started" ping within a
minute of the login screen clearing, and `/doctor` should be all ✅/➖.

## Troubleshooting

- **No startup ping after reboot:** `launchctl print gui/$(id -u)/com.clawdbot.bot` — if it's
  missing, the plist didn't load (check paths in it). If `last exit code` is non-zero, read
  `~/Library/Logs/clawdbot/stderr.log`.
- **`/doctor` says permission denied on chat.db:** Full Disk Access was granted to the wrong
  binary. Re-check the `readlink -f` path; after Python upgrades the path changes.
- **Gmail ❌ `invalid_grant` again after a week:** the OAuth app is still in Testing (step 5).
- **Bot restarts every 10 s:** crash loop; `ThrottleInterval` keeps it from spinning. Run it by
  hand (step 4) to see the traceback.
