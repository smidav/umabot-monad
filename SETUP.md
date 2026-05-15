# Umamusume Tally Bot — Setup & Testing Guide

---

## Part 1: Create the Discord Bot

### 1. Go to the Discord Developer Portal
Navigate to https://discord.com/developers/applications and log in.

### 2. Create a new application
- Click **New Application** (top right)
- Give it a name (e.g. "Uma Tally Bot") and click **Create**

### 3. Create the bot user
- In the left sidebar, click **Bot**
- Click **Add Bot** → **Yes, do it!**
- Under the bot's username, click **Reset Token**, confirm, then **copy the token**
  - ⚠️ This is shown only once. Paste it somewhere safe — you'll need it for `.env`

### 4. Enable the Message Content Intent
Still on the **Bot** page, scroll down to **Privileged Gateway Intents** and enable:
- ✅ **Message Content Intent**

Click **Save Changes**.

### 5. Invite the bot to your server
- In the left sidebar, click **OAuth2 → URL Generator**
- Under **Scopes**, check: `bot`
- Under **Bot Permissions**, check:
  - `Read Messages / View Channels`
  - `Send Messages`
  - `Attach Files`
- Copy the generated URL at the bottom, open it in your browser, and invite the bot to your server

---

## Part 2: Configure the Bot Locally

### 1. Install system dependency (Tesseract)
**Ubuntu/Debian:**
```bash
sudo apt install tesseract-ocr
```
**macOS:**
```bash
brew install tesseract
```
**Windows:**
Download and run the installer from https://github.com/UB-Mannheim/tesseract/wiki
Then add the install directory (e.g. `C:\Program Files\Tesseract-OCR`) to your PATH.

### 2. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file
Copy the example and fill it in:
```bash
cp .env.example .env
```

Open `.env` and set your values:
```
DISCORD_TOKEN=paste_your_bot_token_here

# Optional — paste the channel ID to restrict the bot to one channel.
# To get a channel ID: in Discord, enable Developer Mode under
# User Settings → Advanced, then right-click the channel → Copy Channel ID
CHANNEL_ID=

# Optional — your own Discord user ID, to restrict !uma reset to you.
# Right-click your own username in Discord → Copy User ID
ADMIN_ID=
```

### 4. Confirm the file layout
Your project folder should look like this:
```
umabot/
├── bot.py
├── ocr.py
├── tally.py
├── constants.py
├── requirements.txt
├── .env
└── tally_data.json     ← created automatically on first use
```

---

## Part 3: Run the Bot

```bash
python bot.py
```

You should see:
```
✅ Logged in as Uma Tally Bot#1234 (ID: 123456789)
   Watching all channels for images.
[ocr] Initialising EasyOCR...    ← only on first run
```

Leave this terminal running. The bot is live as long as this process is alive.

---

## Part 4: Test on Discord

Open Discord and go to your server.

### Test 1 — Image submission
Post your race screenshot directly in the channel (drag-and-drop or attach).
The bot should reply within a few seconds with something like:

```
✅ Race recorded! Enemy characters found:

1. Oguri Cap — Pace
2. Seiun Sky — Front *(corrected)*
3. Grass Wonder — Late *(corrected)*
4. Agnes Tachyon — Pace *(corrected)*
5. Oguri Cap — Pace *(corrected)*
6. Eishin Flash — Late *(corrected)*

Tally now covers 1 race(s). Use !uma tally to view.
```

*(corrected)* means the raw OCR output was snapped to a canonical name — this is normal and expected for most cards.

### Test 2 — View the tally
```
!uma tally
```
Expected: a formatted table showing character names, total appearances, and strategy breakdowns.

### Test 3 — Export CSV
```
!uma export
```
Expected: the bot posts a `umamusume_tally.csv` file you can download.

### Test 4 — Undo last entry
```
!uma undo
```
Expected: the bot confirms it removed the last race's entries. Run `!uma tally` again to confirm the count dropped by 1.

### Test 5 — Help
```
!uma help
```
Expected: an embed listing all commands.

### Test 6 — Reset (if ADMIN_ID is set to your ID)
```
!uma reset
```
The bot asks for confirmation. Reply:
```
!uma reset confirm
```
Expected: all data wiped.

---

## Troubleshooting

**Bot comes online but doesn't respond to images**
- Check that **Message Content Intent** is enabled in the Developer Portal (Part 1, Step 4)
- If `CHANNEL_ID` is set in `.env`, make sure you're posting in that specific channel

**`ModuleNotFoundError: No module named 'pytesseract'`**
```bash
pip install pytesseract
```

**`TesseractNotFoundError`**
Tesseract binary isn't on your PATH. Re-check Part 2, Step 1. On Windows, confirm the install directory is in your system PATH and restart your terminal.

**Bot is online but OCR returns 6 "Unknown" names**
Run the debug helper to check that card regions are being detected correctly:
```python
# Run this separately, not as part of the bot
from ocr import save_debug_image
save_debug_image("your_screenshot.png", "debug.png")
```
Open `debug.png` — enemy cards should have green outlines, player cards yellow.
If all cards are unlabelled, the background HSV thresholds may need adjustment for your specific device's screenshot colour profile.

**Strategy returns "Unknown" for one or more cards**
The strategy pill may be at a slightly different vertical position. Check the crop by saving the card slice:
```python
import cv2
img = cv2.imread("your_screenshot.png")
# Find card x-bounds from save_debug_image output, then:
card = img[:, x1:x2+1]
cv2.imwrite("card_debug.png", card)
```
Open `card_debug.png` and measure where the strategy text sits as a percentage of card height, then adjust the `0.48` / `0.84` values in `ocr_strategy()`.
