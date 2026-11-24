# Sudu Café Bot: setup & daily use

The bot lives in your Telegram groups, saves everything to the **SuduBot** Google Sheet,
and runs 24/7 on the Google Cloud server. Your PC does not need to be on.

## What it does

| Where | Who | What |
|---|---|---|
| Staff group | everyone | 🧾 receipt photos → Sheet · 📊 POS closing photo → Daily Sales · 📦 stock counts · 🛒 shopping list · 📖 recipes & checklists |
| Owners chat | owners | all of the above + 💰 `/spend`, `/sales`, `/payback`, money questions |

Money is **never** shown to staff: the bot doesn't even read money data when a staff member asks.

## How staff use it

- **Receipt:** send a photo → the bot shows what it read → tap **✅ Save**.
  Wrong? Reply to the bot's message: `2 qty 12` · `2 price 7.29` · `2 total 87.48` · `2 name Low Fat Milk 1L` ·
  `2 cat consumables` · `delete 3` · `add Ice 2 x 5.00` · `date 12/11` · `shop Giant` · `total 152.95` · `paid by Ali`
  (several at once: one per line). If the numbers don't add up, the bot says so before you save.
  The same receipt can't be saved twice by accident.
- **Daily sales:** send the POS end-of-day report photo → **✅ Save** (fix with `total 2400`, `cash 512.40`, `date 23/11`).
- **Stock:** `/stock` = what's low · `/count` = full list · update with `stock full cream milk 120, condensed 17` → **✅ Update**.
  Anything that drops below its minimum goes onto the shopping list automatically.
- **Shopping:** `/buy` = list (tap an item when bought) · `buy ice, straws` = add.
  Items on a saved receipt are ticked off automatically.
- **Recipes & SOP:** `/recipe cendol` · `/checklist opening` · `/ask how much coconut milk for 4L cendol?`

Owners also have `/spend`, `/sales`, `/payback` (add a month: `/spend oct`, `/payback last`).

## The Google Sheet

| Tab | What's in it | Who writes |
|---|---|---|
| Receipts | one row per receipt (ID, date, shop, total, paid by, photo link) | bot |
| Receipt Items | one row per item on each receipt | bot |
| Daily Sales | one row per day | bot |
| Stock | Item · Current · Min · Unit · Location | bot + you (edit Min/Unit/Location freely) |
| Shopping List | items to buy and when they were bought | bot |
| Summary | monthly spending, sales and who-paid tables (formulas) | automatic |
| Bingsu Recipes, Other Recipes, Checklists, Inspection | your reference info | you |

You can safely edit numbers or fix typos in any row. **Don't rename the column headers or the tabs**:
the bot checks them and will refuse to write (and tell you) if they change.

## Going live (one time)

Do this after closing time; it takes about 10 minutes.

1. **Back up your keys**: download `~/cafe_bot/.env` and `~/cafe_bot/credentials.json` from the server
   (SSH window → ⚙️ → Download file) and keep them somewhere private. Never upload them to GitHub.
2. **Update `.env` on the server** (`nano ~/cafe_bot/.env`). It needs:
   ```
   TELEGRAM_BOT_TOKEN=...        (unchanged)
   GEMINI_API_KEY=...            ← the NEW free key
   OWNER_GROUP_ID=-100...        (/chatid in the owners chat)
   STAFF_GROUP_ID=-100...        (/chatid in the staff group)
   OWNER_USER_IDS=123,456        (/whoami for each owner)
   SPREADSHEET_ID=1W5KA...       (from the Sheet's web address)
   ```
3. **Merge `v2` into `main` on GitHub.** The server picks it up within a minute and restarts.
4. **Clean up the Sheet**, on the server:
   ```
   cd ~/cafe_bot && source venv/bin/activate
   python migrate_sheet.py           # shows what will change, changes nothing
   python migrate_sheet.py --apply   # does it (saves a backup file first; old tabs are renamed "OLD …", not deleted)
   ```
5. In Telegram, send `/help` in both groups, then a test receipt photo.

Check it's running: `sudo systemctl status cafebot` · live log: `sudo journalctl -u cafebot -f`.

## If something goes wrong

- **"AI is busy"**: the free Gemini limit or a busy model; wait a minute. Optional backup: add a free
  `GROQ_API_KEY` (console.groq.com) to `.env`.
- **"tab has different columns"**: someone renamed a header; put it back, or run `migrate_sheet.py`.
- **Undo the update**: on GitHub, revert the merge; the server goes back within a minute.

## For developers

- `bot.py` Telegram handlers · `ai_chat.py` Gemini calls with fixed JSON schemas · `google_integration.py`
  the Sheet (fixed headers, append-only) · `storage.py` pure data rules (dates, numbers, receipts) ·
  `migrate_sheet.py` one-off layout migration. (The four main file names are kept because the server's
  auto-update checks them.)
- Tests: `pip install -r requirements.txt pytest && pytest -q` (no network needed).
- Secrets guard: `git config core.hooksPath .githooks` once; `check_secrets.py` blocks keys in commits
  and runs on GitHub for every push.
