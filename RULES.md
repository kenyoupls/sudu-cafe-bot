# Sudu Café Bot: agreed rules

The owner's decisions for how the bot behaves. Code changes must keep these true.
(IDs and keys live only in the server's `.env`, never here; this repo is public.)

## Chats and who sees what
- One bot, two chats: the **owners chat** (everything) and the **staff group** (no money).
- Owners are listed by Telegram user ID in `OWNER_USER_IDS` (co-owners can be added later).
- In the staff group the bot is a normal member. It replies only when tagged or replied to,
  except receipt/sales photos and pasted stock checklists, which it always reads. It never butts in on chat.
- Strangers (other groups, DMs from non-owners) are ignored.

## Money is owners-only
- In the staff group, questions about sales, revenue, profit, expenses, costs, earning, spending,
  income, budget, salary, wages, gaji, who paid, monthly totals, financial info, "how much did/do we…",
  and **staff working hours / overtime** get: *"Financial info is only available in the owner group. Check with the boss."*
  This is decided by code (`storage.MONEY_PATTERNS`), not left to the AI, and the AI never receives money data there.
- Owner commands (`/spend`, `/sales`, `/payback`) are refused the same way in the staff group.
- **Opening hours are public**: `/hours` and "are we open Wednesday?" work for everyone (live from Google).
- The refusal has **🔓 Show anyway (owner only)**. A non-owner tap does nothing. An owner tap answers,
  marks "✅ Overridden by <owner>", and keeps owner mode for follow-ups (15 min max, 5 min idle).
  It closes on "done", "thanks", "close", "no more" or "end".
- Refusals are never stored in the chat memory, so the bot doesn't start parroting them.

## Memory and understanding
- Each chat remembers its own last 3 days, so follow-ups ("what about Friday?") make sense.
  Owners-chat talk never reaches the staff group.
- Read like a human, don't hardcode: free-form replies ("the milk should be 10 not 12, Ali paid",
  "wrong photo, buang je", "beli kat Giant semalam RM18.90") are understood by the AI.
- Honesty: the AI never claims it saved or changed anything (only buttons and code do that), never invents
  where data is stored ("check the Google Sheet"), and asks to rephrase a bare "ok"/"yes" with no context.
- The same prompts are used for Gemini (primary) and Groq (backup).

## Receipts, sales, expenses
- Photo → the bot shows what it read → nothing is saved until **✅ Save** (or a typed "ok/save").
- Items must add up to the total; if not, a second, stronger read; still off → a clear warning.
- The same receipt can't be saved twice by accident (same photo, or same shop + date + total).
- Shopping-list items on a saved receipt are ticked off automatically.

## Stock
- The Google Sheet is the source of truth and is re-read every time.
- Paper checklists (8+ `Name: number` lines, or 4+ with a "stock check"/"checked by" header) are read by
  code, so no item is dropped. The AI only reads lines code can't (e.g. "eighty-eight") and matches unfamiliar names.
- Blank lines are asked about ("left blank: … reply zero / skip / counts"), never silently set to 0.
- `OK`, `LOW`, `YES` are refused (a number or OUT is needed). `out`, `OOS`, `habis`, `nil`, `none` = 0.
- Exact name match first, so "Brown Boba (Cook)" and "Brown Boba (Jelly)" never collide.
- An item not on the list: "Is this a regular stock item?" (Yes/No, or a typed yes/no), keeping the qty given.
  Then "Was it bought? Add to expenses?", answered naturally ("yes RM15 FairPrice paid by me").
- Several new items can wait at once. Replies match by Telegram reply first, then a single open question in that chat.
- Replying "actually 92" to a "Stock updated" message fixes that item.
- Items dropping below their minimum go onto the shopping list.

## Waiting tasks and nagging
- Anything waiting for an answer (unsaved receipts and sales, stock questions, expense questions) survives a restart
  and never expires.
- One reminder a day at **12 PM** lists everything still waiting. No other reminders.
- Gone for good: the old action-items / chase-up system, the "one-off" item concept, 10-minute expiry, daily nags.

## How we work
- Plan first and get the owner's OK before changing code; explain in plain language.
- Tests run before anything goes live; the owner isn't asked to test every scenario by hand.
- Keys (`.env`, `credentials.json`) stay on the server only; a secret check blocks them from GitHub.

## Parked
- Price-jump alerts on receipts · dashboard · co-owner IDs (add to `OWNER_USER_IDS` when available).
