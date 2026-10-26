"""Combined PlayStation voucher availability monitor."""

import asyncio
import argparse
import os
import re
import sys
from datetime import datetime

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


CHECK_INTERVAL = 20
SLOW_CHECK_SECONDS = 15
HEALTH_SUMMARY_EVERY = 10
NAVIGATION_TIMEOUT_MS = 30_000
PAGE_TIMEOUT_MS = 5000
CHECK_TIMEOUT_SECONDS = 60
MAX_CONCURRENT_CHECKS = 6
DENOMINATIONS = (1000, 2000, 3000)

# Substrings of an "unknown" detail that indicate a real fault (counted as errors)
# rather than an ordinary unrecognised page state.
ERROR_DETAIL_MARKERS = (
    "failed", "timeout", "unreachable", "http ", "no http response",
    "could not be inspected", "could not be verified", "could not be launched",
    "security challenge", "login wall", "access denied",
)

GYFTR_URL = "https://www.gyftr.com/sony-playstation"
ROOTER_URL = "https://shop.rooter.gg/product/playstation-gift-cards"
BLINKIT_URL = "https://blinkit.com/prn/sony-playstation-instant-voucher/prid/698590"
STANSHOP_URL = "https://www.stanshop.co/playstation-psn-gift-card-in-india"
DIGISTORE_URL = "https://digistore.co.in/product-category/console-games/playstation-card/"
SWAG_URL = "https://www.swag.gg/gift-cards/playstation"
ALERTS_PER_RESTOCK = 1          # notifications sent before the cooldown starts
ALERT_COOLDOWN_SECONDS = 300    # default pause after notifications before alerting again
SITE_COOLDOWN_SECONDS = {       # per-site overrides, keyed by site name
    "StanShop": 3 * 3600,       # 3 hour timer after an alert
}
# Sites whose timer is reset by a genuine restock. If the item is seen out of stock
# while the timer is active and then comes back, it must be detected as available
# on this many consecutive checks before a new alert is sent (guards against false
# alerts); the alert then restarts the timer. Sites not listed here keep the normal
# behaviour: nothing is sent while their timer is running.
# GamesTheShop is deliberately not listed: it alerts on its very first detection.
RESTOCK_RESET_CONFIRMATIONS = {
    "StanShop": 2,
}

# Memory management for long sessions. With CHECK_INTERVAL = 20s, 30 cycles is
# roughly 10 minutes and 180 cycles roughly an hour.
PAGE_RECYCLE_CYCLES = 30        # close and reopen every tab (frees leaked JS heap/DOM)
BROWSER_RECYCLE_CYCLES = 180    # also recreate the browser context
CHROMIUM_ARGS = [
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--mute-audio",
    "--no-first-run",
]
GAMES_THE_SHOP = {
    1000: "https://www.gamestheshop.com/product/2153a461-7203-4802-aac4-283be4c775d3?variant=09df672e-b598-4b2d-9b3e-e1ca1dd9d205",
    2000: "https://www.gamestheshop.com/product/d117bc23-1c4b-4330-b7f3-f90c71d9a5b3?variant=cb05d659-beac-47d7-85ff-8bfed95f47cf",
    3000: "https://www.gamestheshop.com/product/f528de33-a760-4fea-9b04-3d150b24a116?variant=7e6631a0-0d65-4907-89b7-4ab5e24397cc",
}


def rooter_tile_state(lines, amount):
    """Read one denomination tile from Rooter's visible page text.

    Tiles render as "Rs.1000 Wallet Code" followed by a status line such as
    "Out of Stock". Returns (available, detail); available is None when unsure.
    """
    tile = re.compile(rf"^rs\.?\s*{amount}\s+wallet code\b", re.I)
    any_tile = re.compile(r"^rs\.?\s*[\d,]+\s+wallet code\b", re.I)
    for i, line in enumerate(lines):
        if not tile.search(line):
            continue
        segment = [line]
        for following in lines[i + 1:i + 4]:
            # Stop at the next tile or at the purchase widget below the tiles,
            # whose own "Out of Stock" button belongs to the selected option.
            if any_tile.search(following) or following.lower().startswith("add delivery info"):
                break
            segment.append(following)
        text = " ".join(segment).lower()
        if re.search(r"\b(out of stock|sold out)\b", text):
            return False, "voucher tile says out of stock"
        if re.search(r"\b(unavailable|not available|coming soon|notify)\b", text):
            return None, "voucher tile shows a non-orderable state"
        return True, "voucher tile shows no out-of-stock marker"
    return None, "voucher tile not found"


def stanshop_tile_state(texts):
    """Read one denomination tile from StanShop's variant selector.

    Each tile is a link whose text starts with a badge: "SOLD OUT" when empty,
    or a discount badge such as "5% OFF" when orderable. `texts` holds the text
    of every visible link for that denomination. Returns (available, detail);
    available is None when unsure.
    """
    if not texts:
        return None, "denomination tile not found"
    states = set()
    for text in texts:
        lowered = text.lower()
        if re.search(r"\b(sold out|out of stock)\b", lowered):
            states.add(False)
        elif re.search(r"\b(unavailable|not available|coming soon|notify)\b", lowered):
            states.add(None)
        else:
            states.add(True)
    if states == {False}:
        return False, "tile says SOLD OUT"
    if states == {True}:
        return True, "tile shows no sold-out marker"
    return None, "tile state is ambiguous"


def digistore_card_state(cards, amount):
    """Read one denomination's product card from Digistore's category page.

    `cards` holds dicts with title, text, hasCart and hasPrice for each visible
    product card. Out-of-stock cards carry an "Out of Stock" label; orderable
    ones show a price and/or an add-to-cart control. Returns (available, detail);
    available is None when unsure.
    """
    pattern = re.compile(rf"\brs\.?\s*{amount}\b", re.I)
    matches = [card for card in cards
               if pattern.search(card["title"]) and re.search(r"psn|playstation", card["title"], re.I)]
    if not matches:
        return None, "product card not found"
    if len(matches) > 1:
        return None, "product card is ambiguous"
    card = matches[0]
    text = card["text"].lower()
    if re.search(r"\b(out of stock|sold out)\b", text):
        return False, "product card says Out of Stock"
    if re.search(r"\b(coming soon|notify|unavailable|read more)\b", text) and not card["hasCart"]:
        return None, "product card shows a non-orderable state"
    if card["hasCart"] or card["hasPrice"]:
        return True, "product card shows no out-of-stock marker"
    return None, "product card state is ambiguous"


def swag_state(lines, amount):
    """Read one denomination from Swag's PlayStation page (lowercased visible lines).

    The page shows a single "Out of stock" / "Notify me" block when nothing can be
    bought. Only a standalone purchase button counts as a buy control; the word
    "buy" inside descriptions, FAQs or links must never count. Returns
    (available, detail); available is None when unsure.
    """
    if not any("playstation" in line for line in lines):
        return None, "PlayStation product not identified"
    buy_control = re.compile(r"(add to cart|buy now|buy|add to bag|proceed to checkout)")
    buy_line = next((line for line in lines if buy_control.fullmatch(line)), None)
    has_buy = buy_line is not None
    page_out = any(re.fullmatch(r"(out of stock|notify me)", line) for line in lines)
    if page_out:
        # An "Out of stock" / "Notify me" block always wins: the page's header card
        # also shows a "₹1000" graphic and other controls that must not count.
        return False, "page says out of stock"
    tile = re.compile(rf"(?:₹|rs\.?\s*){amount}(?:\.0+)?(?:\s.*)?")
    tile_index = None
    for i, line in enumerate(lines):
        flat = line.replace(",", "")
        if len(flat) <= 25 and tile.fullmatch(flat):
            tile_index = i
            break
    if tile_index is None:
        return None, "denomination tile not found"
    nearby = " ".join(lines[tile_index:tile_index + 4])
    if re.search(r"\b(out of stock|sold out|unavailable|notify me)\b", nearby):
        return False, "denomination tile says out of stock"
    if has_buy:
        return True, f"denomination tile '{lines[tile_index]}' and buy control '{buy_line}' are shown"
    return None, "no buy control found for the denomination"


def _timestamp():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _credentials():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment."
        )
    return token, chat_id


def _post(service, url, **kwargs):
    try:
        response = requests.post(url, timeout=10, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            raise RuntimeError(f"{service} returned HTTP {status}") from None
        raise RuntimeError(f"{service} request failed ({type(exc).__name__})") from None


def send_telegram(message):
    token, chat_id = _credentials()
    _post("Telegram API", f"https://api.telegram.org/bot{token}/sendMessage",
          data={"chat_id": chat_id, "text": message})


def send_discord(message):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False
    _post("Discord webhook", webhook_url,
          json={"content": message, "allowed_mentions": {"parse": []}})
    return True


def print_discord_status():
    if os.getenv("DISCORD_WEBHOOK_URL"):
        print("Discord notifications: enabled.")
    else:
        print("⚠️ Discord notifications: disabled (DISCORD_WEBHOOK_URL is not set).")


class CombinedMonitor:
    def __init__(self, browser_context, playwright=None, dry_run=False, browser=None):
        self.browser = browser
        self.browser_context = browser_context
        self.playwright = playwright
        self.dry_run = dry_run
        self.pages = {}
        self.states = {}
        self.alert_attempted = set()
        self.alert_delivered = {}
        self.alert_counts = {}
        self.alert_cooldown_until = {}
        self.went_out_during_timer = {}   # key -> seen out of stock while its timer was active
        self.restock_confirmations = {}   # key -> consecutive available checks since that
        self.cycles_completed = 0
        self.error_count = 0
        self.check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    @staticmethod
    def _configure_page(page):
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    async def route_nonessential_resources(self, route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def park_pages(self):
        """Navigate idle tabs to about:blank so each site's DOM and JS heap can be
        garbage-collected while the monitor waits for the next cycle."""
        pages = list(self.pages.values())

        async def park(page):
            try:
                if not page.is_closed():
                    await page.goto("about:blank", timeout=5000)
            except Exception:
                pass

        await asyncio.gather(*(park(page) for page in pages))

    async def recycle_pages(self):
        """Close every tab; they are reopened lazily by the next checks."""
        pages = list(self.pages.values())
        self.pages.clear()
        for page in pages:
            try:
                await page.close()
            except Exception:
                pass

    async def recycle_browser(self):
        """Throw away the browser context (cache, cookies, service workers)."""
        await self.recycle_pages()
        if self.browser is None:
            return
        old_context = self.browser_context
        new_context = await self.browser.new_context()
        await new_context.route("**/*", self.route_nonessential_resources)
        self.browser_context = new_context
        try:
            await old_context.close()
        except Exception:
            pass

    async def page_for(self, key):
        if key not in self.pages:
            page = await self.browser_context.new_page()
            self._configure_page(page)
            self.pages[key] = page
        return self.pages[key]

    async def load(self, key, url):
        page = await self.page_for(key)
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        # DOM readiness plus a short settle allows client-rendered controls to appear.
        await page.wait_for_timeout(900)
        return page

    async def alert(self, key, site, amount, url, detection):
        stamp = _timestamp()
        detection_text = (
            "Detected as available (not added to cart)"
            if detection == "available"
            else detection
        )
        message = (
            "Possible restock\n"
            f"Website: {site}\n"
            f"Product: PlayStation ₹{amount} Gift Card\n"
            f"Time: {stamp}\n"
            f"Detection: {detection_text}\n"
            f"{url}"
        )
        if self.dry_run:
            print(f"[dry-run] Would send Telegram alert:\n{message}")
            return True

        delivered = self.alert_delivered.setdefault(key, set())
        if "telegram" not in delivered:
            try:
                await asyncio.to_thread(send_telegram, message)
                delivered.add("telegram")
                print(f"📱 Telegram alert sent: {site} ₹{amount}")
            except Exception as exc:
                print(f"⚠️ Telegram alert failed for {site} ₹{amount} ({type(exc).__name__})")

        discord_configured = bool(os.getenv("DISCORD_WEBHOOK_URL"))
        if discord_configured and "discord" not in delivered:
            try:
                sent = await asyncio.to_thread(send_discord, message)
                if sent:
                    delivered.add("discord")
                    print(f"💬 Discord alert sent: {site} ₹{amount}")
            except Exception as exc:
                print(f"⚠️ Discord alert failed for {site} ₹{amount} ({type(exc).__name__})")

        return "telegram" in delivered and (not discord_configured or "discord" in delivered)

    async def record(self, key, site, amount, url, available, detail="", detection="available"):
        if available is None:
            self.states[key] = "unknown"
            detail_lower = detail.lower()
            if any(marker in detail_lower for marker in ERROR_DETAIL_MARKERS):
                self.error_count += 1
            print(f"⚪ {site} ₹{amount}: unknown{': ' + detail if detail else ''}")
            return
        self.states[key] = "available" if available else "unavailable"
        if available:
            print(f"🟢 {site} ₹{amount}: possible restock{': ' + detail if detail else ''}")
            now = asyncio.get_running_loop().time()
            cooldown_until = self.alert_cooldown_until.get(key)
            if cooldown_until is not None:
                if now < cooldown_until:
                    needed = RESTOCK_RESET_CONFIRMATIONS.get(site)
                    if needed and self.went_out_during_timer.get(key):
                        # Went out of stock during the timer and is back: only alert
                        # once it has been detected as available enough times in a row.
                        confirmations = self.restock_confirmations.get(key, 0) + 1
                        self.restock_confirmations[key] = confirmations
                        if confirmations < needed:
                            self.states[key] = "available"
                            print(f"⏳ {site} ₹{amount}: back in stock, confirming ({confirmations}/{needed})")
                            return
                        if await self.alert(key, site, amount, url,
                                            f"Back in stock, confirmed on {needed} consecutive checks"):
                            self.alert_delivered.pop(key, None)
                            self.alert_attempted.add(key)
                            self.states[key] = "alerted"
                            cooldown = SITE_COOLDOWN_SECONDS.get(site, ALERT_COOLDOWN_SECONDS)
                            self.alert_cooldown_until[key] = now + cooldown
                            self.went_out_during_timer.pop(key, None)
                            self.restock_confirmations.pop(key, None)
                            print(f"🔔 {site} ₹{amount}: restock confirmed, timer reset ({cooldown // 60} min)")
                        else:
                            # Keep the confirmations so delivery is retried next check.
                            self.error_count += 1
                        return
                    remaining = int(cooldown_until - now)
                    self.states[key] = "alerted"
                    print(f"⏸ {site} ₹{amount}: alert cooldown, {remaining}s remaining")
                    return
                # Cooldown finished and it is still in stock: start a new round.
                del self.alert_cooldown_until[key]
                self.alert_counts[key] = 0
                self.went_out_during_timer.pop(key, None)
                self.restock_confirmations.pop(key, None)
            if self.alert_counts.get(key, 0) < ALERTS_PER_RESTOCK:
                if await self.alert(key, site, amount, url, detection):
                    self.alert_delivered.pop(key, None)
                    self.alert_attempted.add(key)
                    self.states[key] = "alerted"
                    self.alert_counts[key] = self.alert_counts.get(key, 0) + 1
                    print(f"🔔 {site} ₹{amount}: alert {self.alert_counts[key]}/{ALERTS_PER_RESTOCK}")
                    if self.alert_counts[key] >= ALERTS_PER_RESTOCK:
                        cooldown = SITE_COOLDOWN_SECONDS.get(site, ALERT_COOLDOWN_SECONDS)
                        self.alert_cooldown_until[key] = now + cooldown
                        print(f"⏸ {site} ₹{amount}: cooldown started ({cooldown // 60} min)")
                else:
                    self.error_count += 1
        else:
            self.alert_attempted.discard(key)
            self.alert_delivered.pop(key, None)
            if site in RESTOCK_RESET_CONFIRMATIONS:
                cooldown_until = self.alert_cooldown_until.get(key)
                if cooldown_until is not None and asyncio.get_running_loop().time() < cooldown_until:
                    # Remember the dip; any partial confirmation streak starts over.
                    self.went_out_during_timer[key] = True
                    self.restock_confirmations[key] = 0
            # alert_counts and alert_cooldown_until are deliberately kept, so going
            # out of stock does not reset the alert limit or the cooldown by itself.
            print(f"🔴 {site} ₹{amount}: unavailable")

    async def record_all(self, site, amounts, url, result, detail):
        for amount in amounts:
            await self.record(f"{site}:{amount}", site, amount, url, result, detail)

    async def check_gyftr(self):
        page = await self.load("GyFTR", GYFTR_URL)
        try:
            await page.get_by_text("No thanks", exact=True).click(timeout=700)
        except Exception:
            pass
        snapshot = await page.evaluate("""amounts => {
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = el => !!(el && el.getClientRects().length &&
            getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
          const body = norm(document.body?.innerText || '').toLowerCase();
          const sold = /sorry!!!\\s*we are sold out|no product found/i.test(body);
          const productHeading = /playstation/i.test(norm(document.querySelector('h1')?.innerText || document.title));
          const candidates = [...document.querySelectorAll(
            'button, [role="button"], input, select, option, label, [class*="denomination"], [class*="amount"], [class*="variant"]')]
            .filter(visible);
          const exposed = {};
          for (const amount of amounts) {
            const pattern = new RegExp('^(?:₹|rs\\\\.?\\\\s*)?' + amount + '(?:\\\\.00)?$', 'i');
            exposed[amount] = candidates.some(el => {
              const label = norm(el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/,/g, '');
              if (!pattern.test(label) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
              let ancestor = el;
              for (let i = 0; i < 8 && ancestor && ancestor !== document.body; i++, ancestor = ancestor.parentElement) {
                const context = norm(ancestor.innerText || '');
                if (context.length <= 2500 && /playstation/i.test(context)) return true;
              }
              return false;
            });
          }
          return {sold, productHeading, exposed};
        }""", list(DENOMINATIONS))
        for amount in DENOMINATIONS:
            result = False if snapshot["sold"] else (
                True if snapshot["productHeading"] and snapshot["exposed"].get(str(amount), False) else None
            )
            detail = ("page reports the PlayStation voucher is sold out" if snapshot["sold"]
                      else "PlayStation denomination control is exposed" if result
                      else "denomination control not exposed within the PlayStation product")
            await self.record(f"GyFTR:{amount}", "GyFTR", amount, GYFTR_URL, result,
                              detail)

    async def check_rooter(self):
        page = await self.load("Rooter", ROOTER_URL)
        try:
            # The denomination tiles render client-side after an API call, which can
            # take several seconds. Polling returns as soon as they appear.
            await page.wait_for_function("""amounts => {
              const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
              return amounts.every(amount =>
                new RegExp('Rs\\\\.?\\\\s*' + amount + '\\\\s+Wallet Code', 'i').test(text));
            }""", arg=list(DENOMINATIONS), timeout=10000, polling=250)
        except PlaywrightTimeoutError:
            # Missing tiles are reported as UNKNOWN below.
            pass

        async def read_states():
            text = await page.locator("body").inner_text()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return {amount: rooter_tile_state(lines, amount) for amount in DENOMINATIONS}

        states = await read_states()
        if any(result is True for result, _ in states.values()):
            # Confirm after a short pause so a tile that rendered a moment before
            # its "Out of Stock" label does not trigger a false alert.
            await page.wait_for_timeout(800)
            states = await read_states()
        for amount, (result, detail) in states.items():
            await self.record(f"Rooter:{amount}", "Rooter", amount, ROOTER_URL, result, detail)

    async def check_blinkit(self):
        page = await self.load("Blinkit", BLINKIT_URL)
        text = [line.strip().lower() for line in (await page.locator("body").inner_text()).splitlines() if line.strip()]
        if not any("playstation" in line or "voucher" in line for line in text):
            await self.record_all("Blinkit", DENOMINATIONS, BLINKIT_URL, None, "product content missing")
            return
        first_index = {}
        for i, line in enumerate(text):
            first_index.setdefault(line, i)
        for amount in DENOMINATIONS:
            labels = (str(amount), f"₹{amount}", f"rs {amount}", f"rs.{amount}")
            i = min((first_index[label] for label in labels if label in first_index), default=None)
            found = i is not None
            available = None
            if found:
                nearby = " ".join(text[i + 1:i + 5])
                if re.search(r"\b(out of stock|sold out|unavailable|not available|not in stock)\b", nearby):
                    available = False
                elif re.search(r"\b(coming soon|notify me|notify when|pre[- ]?order)\b", nearby):
                    available = None
                elif re.search(r"\b(in stock|available now|available)\b", nearby):
                    available = True
                else:
                    available = None
            await self.record(f"Blinkit:{amount}", "Blinkit", amount, BLINKIT_URL,
                              available if found else None,
                              "denomination not found" if not found else
                              "no explicit stock signal near the denomination" if available is None else "")

    async def check_gamestheshop(self, amount, url):
        key = f"GamesTheShop:{amount}"
        page = await self.load(key, url)
        try:
            # The product details (including the "Availability:" row) are rendered
            # client-side after the document loads, so wait for them explicitly.
            await page.wait_for_function(
                "() => (document.body?.innerText || '').toLowerCase().includes('availability:')",
                timeout=6000, polling=250)
        except PlaywrightTimeoutError:
            await self.record(key, "GamesTheShop", amount, url, None, "product details did not render")
            return
        text = (await page.locator("body").inner_text()).lower()
        # Read the product's own "Availability:" row. Page-wide phrases such as the
        # footer newsletter's "Notify Me" button appear on every page and must not
        # be treated as stock signals.
        match = re.search(r"availability:\s*([^\n]+)", text)
        availability = match.group(1).strip() if match else ""
        if "playstation" not in text:
            result, detail = None, "PlayStation product not identified"
        elif re.search(r"\b(out of stock|sold out|unavailable|not available|not in stock|coming soon)\b", availability) \
                or "currently out of stock" in text:
            result, detail = False, "product page says out of stock"
        elif re.search(r"\b(in stock|available)\b", availability):
            result, detail = True, f"availability row says '{availability}'"
        else:
            result, detail = None, f"unrecognised availability value '{availability}'"
        await self.record(key, "GamesTheShop", amount, url, result, detail if result is None or result else "")

    async def check_stanshop(self):
        page = await self.load("StanShop", STANSHOP_URL)
        try:
            # The variant tiles can render after hydration; wait until all are present.
            await page.wait_for_function("""amounts => amounts.every(a =>
              document.querySelector(`a[href*="playstation-psn-gift-card-in-india-${a}-inr"]`))""",
                                         arg=list(DENOMINATIONS), timeout=8000, polling=250)
        except PlaywrightTimeoutError:
            # Missing tiles are reported as UNKNOWN below.
            pass

        async def read_states():
            tiles = await page.evaluate("""amounts => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = el => !!(el && el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
              const out = {};
              for (const a of amounts) {
                out[a] = [...document.querySelectorAll(`a[href*="playstation-psn-gift-card-in-india-${a}-inr"]`)]
                  .filter(visible).map(el => norm(el.innerText));
              }
              return out;
            }""", list(DENOMINATIONS))
            return {amount: stanshop_tile_state(tiles.get(str(amount), [])) for amount in DENOMINATIONS}

        states = await read_states()
        if any(result is True for result, _ in states.values()):
            # Confirm after a short pause so a tile that rendered a moment before
            # its "SOLD OUT" badge does not trigger a false alert.
            await page.wait_for_timeout(800)
            states = await read_states()
        for amount, (result, detail) in states.items():
            await self.record(f"StanShop:{amount}", "StanShop", amount, STANSHOP_URL, result, detail)

    async def check_digistore(self):
        page = await self.load("Digistore", DIGISTORE_URL)
        try:
            # Wait for the product grid to render before reading it.
            await page.wait_for_selector(".product", timeout=8000)
        except PlaywrightTimeoutError:
            # An empty grid is reported as UNKNOWN below.
            pass

        async def read_states():
            cards = await page.evaluate("""() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = el => !!(el && el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
              return [...document.querySelectorAll('.product')]
                .filter(card => !card.querySelector('.product') && visible(card))
                .map(card => {
                  const titleEl = card.querySelector('h2, h3, h4, h5, h6, .woocommerce-loop-product__title');
                  const text = norm(card.innerText);
                  return {
                    title: norm(titleEl?.innerText || ''),
                    text,
                    hasCart: !!card.querySelector(
                      'a.add_to_cart_button, a[href*="add-to-cart"], button[name="add-to-cart"]'),
                    hasPrice: /₹\\s*[\\d,]+/.test(text),
                  };
                });
            }""")
            return {amount: digistore_card_state(cards, amount) for amount in DENOMINATIONS}

        states = await read_states()
        if any(result is True for result, _ in states.values()):
            # Confirm after a short pause so a card that rendered a moment before
            # its "Out of Stock" label does not trigger a false alert.
            await page.wait_for_timeout(800)
            states = await read_states()
        for amount, (result, detail) in states.items():
            await self.record(f"Digistore:{amount}", "Digistore", amount, DIGISTORE_URL, result, detail)

    async def check_swag(self):
        page = await self.load("Swag", SWAG_URL)
        try:
            # The page is client-rendered; wait for either the stock banner or a buy control.
            await page.wait_for_function(
                "() => /out of stock|notify me|add to cart|buy now/i.test(document.body?.innerText || '')",
                timeout=8000, polling=250)
        except PlaywrightTimeoutError:
            # Missing content is reported as UNKNOWN below.
            pass

        async def read_states():
            text = await page.locator("body").inner_text()
            lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
            return {amount: swag_state(lines, amount) for amount in DENOMINATIONS}

        states = await read_states()
        if any(result is True for result, _ in states.values()):
            # Confirm after a short pause so a page that rendered a moment before
            # its "Out of stock" label does not trigger a false alert.
            await page.wait_for_timeout(800)
            states = await read_states()
        for amount, (result, detail) in states.items():
            await self.record(f"Swag:{amount}", "Swag", amount, SWAG_URL, result, detail)

    def _mark_check_failed(self, keys):
        self.error_count += 1
        for key in keys:
            self.states[key] = "unknown"

    async def guarded(self, name, check, keys=()):
        async def run_check():
            async with self.check_semaphore:
                await check()
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(run_check(), timeout=CHECK_TIMEOUT_SECONDS)
            elapsed = asyncio.get_running_loop().time() - started
            if elapsed > SLOW_CHECK_SECONDS:
                print(f"⏱ {name}: slow check ({elapsed:.0f}s)")
        except asyncio.TimeoutError:
            self._mark_check_failed(keys)
            print(f"⚠️ {name}: check timed out after {CHECK_TIMEOUT_SECONDS}s")
        except Exception as exc:
            self._mark_check_failed(keys)
            print(f"⚠️ {name}: check failed ({type(exc).__name__})")

    async def cycle(self):
        # Recycle between cycles, while no check is using a page.
        if self.cycles_completed:
            try:
                if self.cycles_completed % BROWSER_RECYCLE_CYCLES == 0:
                    await self.recycle_browser()
                    print("♻️ Browser recycled to release memory")
                elif self.cycles_completed % PAGE_RECYCLE_CYCLES == 0:
                    await self.recycle_pages()
                    print("♻️ Tabs recycled to release memory")
            except Exception as exc:
                print(f"⚠️ Recycle failed ({type(exc).__name__})")
        checks = [
            ("GyFTR", self.check_gyftr, [f"GyFTR:{n}" for n in DENOMINATIONS]),
            ("Rooter", self.check_rooter, [f"Rooter:{n}" for n in DENOMINATIONS]),
            ("Blinkit", self.check_blinkit, [f"Blinkit:{n}" for n in DENOMINATIONS]),
            ("StanShop", self.check_stanshop, [f"StanShop:{n}" for n in DENOMINATIONS]),
            ("Digistore", self.check_digistore, [f"Digistore:{n}" for n in DENOMINATIONS]),
            ("Swag", self.check_swag, [f"Swag:{n}" for n in DENOMINATIONS]),
        ]
        checks.extend((f"GamesTheShop ₹{amount}", lambda a=amount, u=url: self.check_gamestheshop(a, u),
                       [f"GamesTheShop:{amount}"])
                      for amount, url in GAMES_THE_SHOP.items() if url)
        # Limit concurrent navigation/DOM work to reduce resource spikes while
        # still checking independent sites in parallel.
        await asyncio.gather(*(self.guarded(name, check, keys) for name, check, keys in checks))
        await self.park_pages()
        self.cycles_completed += 1
        if self.cycles_completed % HEALTH_SUMMARY_EVERY == 0:
            available = alerts_pending = unavailable = unknown = 0
            for key, state in self.states.items():
                if state in {"available", "alerted"}:
                    available += 1
                    if key not in self.alert_attempted:
                        alerts_pending += 1
                elif state == "unavailable":
                    unavailable += 1
                elif state == "unknown":
                    unknown += 1
            health = "Monitor healthy" if self.error_count == 0 else "Monitor running (errors observed)"
            print(
                f"{health} | Checks completed: {self.cycles_completed} | "
                f"Available: {available} | Unavailable: {unavailable} | Unknown: {unknown} | "
                f"Alerts pending: {alerts_pending} | "
                f"Errors: {self.error_count} | Next check: ~{CHECK_INTERVAL}s"
            )
            self.error_count = 0


async def main(once=False, dry_run=False):
    if not dry_run:
        _credentials()
    print("PlayStation voucher combined monitor")
    print_discord_status()
    for amount, url in GAMES_THE_SHOP.items():
        if not url:
            print(f"⚠️ GamesTheShop ₹{amount}: no URL set, skipping (see GAMES_THE_SHOP).")
    if dry_run:
        print("Read-only dry run: no cart clicks or Telegram messages.")
    else:
        print(f"Checking sites every approximately {CHECK_INTERVAL} seconds. Ctrl+C stops it.")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        browser_context = await browser.new_context()
        monitor = CombinedMonitor(browser_context, playwright=p, dry_run=dry_run, browser=browser)
        await browser_context.route("**/*", monitor.route_nonessential_resources)
        try:
            while True:
                started = asyncio.get_running_loop().time()
                print(f"\n--- Check {_timestamp()} ---")
                await monitor.cycle()
                if once:
                    break
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(1, CHECK_INTERVAL - elapsed))
        finally:
            for name, close in (("Browser context", monitor.browser_context.close),
                                ("Browser", browser.close)):
                try:
                    await close()
                except Exception as exc:
                    print(f"⚠️ {name} cleanup warning: {type(exc).__name__}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor PlayStation voucher availability.")
    parser.add_argument("--once", action="store_true", help="Run one check cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not click Add to cart or send Telegram alerts.")
    arguments = parser.parse_args()
    try:
        asyncio.run(main(once=arguments.once, dry_run=arguments.dry_run))
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
