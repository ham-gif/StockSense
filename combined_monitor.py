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
WOOHOO_URL = "https://www.woohoo.in/playstation-e-gift-instant-voucher"
KRGKART_URL = "https://krgkart.com/product-price/playstation-gift-card/"
KRGKART_DENOMINATIONS = (1000, 2000, 3000, 4000, 5000)
KRGKART_RETRY_SECONDS = 300
KRGKART_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ALERTS_PER_RESTOCK = 1          # notifications sent before the cooldown starts
ALERT_COOLDOWN_SECONDS = 300    # default pause after notifications before alerting again
SITE_COOLDOWN_SECONDS = {       # per-site overrides, keyed by site name
    "Woohoo": 3600,
}

# Memory management for long sessions. With CHECK_INTERVAL = 20s, 30 cycles is
# roughly 10 minutes and 180 cycles roughly an hour.
PAGE_RECYCLE_CYCLES = 30        # close and reopen every tab (frees leaked JS heap/DOM)
BROWSER_RECYCLE_CYCLES = 180    # also recreate the browser context and KRGKart's Chrome
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


class KRGKartBrowserUnavailable(Exception):
    pass


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
        self.cycles_completed = 0
        self.error_count = 0
        self.check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
        self.krg_browser = None
        self.krg_context = None
        self.krg_page = None
        self.krg_retry_after = 0.0

    @staticmethod
    def _configure_page(page):
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    def _defer_krg_retry(self):
        """Back off KRGKart browser launches for KRGKART_RETRY_SECONDS."""
        self.krg_retry_after = asyncio.get_running_loop().time() + KRGKART_RETRY_SECONDS

    async def route_nonessential_resources(self, route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def park_pages(self):
        """Navigate idle tabs to about:blank so each site's DOM and JS heap can be
        garbage-collected while the monitor waits for the next cycle."""
        pages = list(self.pages.values())
        if self.krg_page is not None:
            pages.append(self.krg_page)

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
        if self.krg_page is not None:
            pages.append(self.krg_page)
            self.krg_page = None
        for page in pages:
            try:
                await page.close()
            except Exception:
                pass

    async def recycle_browser(self):
        """Throw away the browser context (cache, cookies, service workers) and
        KRGKart's separate Chrome, which relaunches on its next check."""
        await self.recycle_pages()
        await self.close_krg_browser()
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
                    remaining = int(cooldown_until - now)
                    self.states[key] = "alerted"
                    print(f"⏸ {site} ₹{amount}: alert cooldown, {remaining}s remaining")
                    return
                # Cooldown finished and it is still in stock: start a new round.
                del self.alert_cooldown_until[key]
                self.alert_counts[key] = 0
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
            # alert_counts and alert_cooldown_until are deliberately kept, so going
            # out of stock does not reset the two-alert limit or the cooldown.
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

    async def check_woohoo(self):
        page = await self.load("Woohoo", WOOHOO_URL)
        try:
            # The denomination options render client-side; reading too early made
            # the check report UNKNOWN. Polling returns as soon as they appear.
            await page.wait_for_function("""() => {
              const visible = el => !!(el && el.getClientRects().length);
              return !!document.querySelector(
                  '#desktop-denomination-section .desktop-view__denomination__slabs, #mobile-denomination-slabs-slider .swiper-wrapper')
                || [...document.querySelectorAll('button')].some(b => visible(b) && /out of stock|add to cart/i.test(b.innerText || ''));
            }""", timeout=5000, polling=250)
        except PlaywrightTimeoutError:
            pass
        snapshot = await page.evaluate("""() => {
          const text = el => (el?.innerText || '').replace(/\\s+/g, ' ').trim();
          const visible = el => !!(el && el.getClientRects().length);
          const add = [...document.querySelectorAll('button')].find(b => visible(b) && /add to cart/i.test(text(b) + ' ' + (b.getAttribute('aria-label') || '')));
          const oos = [...document.querySelectorAll('button')].some(b => visible(b) && /out of stock/i.test(text(b) + ' ' + (b.getAttribute('aria-label') || '')))
            || [...document.querySelectorAll('button.product-fixed-second-cta--out-of-stock')].some(visible);
          const denominationSectionPresent = !!document.querySelector('#desktop-denomination-section, #mobile-denomination-section');
          const optionContainerPresent = !!document.querySelector('#desktop-denomination-section .desktop-view__denomination__slabs, #mobile-denomination-slabs-slider .swiper-wrapper');
          const roots = [
            ...document.querySelectorAll('#desktop-denomination-section .desktop-view__denomination__slabs > *, #mobile-denomination-slabs-slider .swiper-wrapper > *')
          ].filter(visible);
          const options = roots.map(el => ({
            text: text(el),
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/i.test(el.className || ''),
            selected: el.getAttribute('aria-pressed') === 'true' || el.getAttribute('aria-selected') === 'true' ||
              [...(typeof el.className === 'string' ? el.className : '').split(/\\s+/)].some(name => /(?:^|[-_])(?:selected|active)$/i.test(name))
          })).filter(option => option.text);
          return { product: /playstation e-gift card/i.test(document.body.innerText), addEnabled: !!add && !add.disabled && add.getAttribute('aria-disabled') !== 'true', oos, options, denominationSectionPresent, optionContainerPresent };
        }""")

        if not snapshot["product"]:
            await self.record_all("Woohoo", DENOMINATIONS, WOOHOO_URL, None, "PlayStation product not identified")
            return

        if snapshot["oos"]:
            await self.record_all("Woohoo", DENOMINATIONS, WOOHOO_URL, False, "product-level Out of Stock control")
            return

        # Strip thousands separators once so "1,000" matches 1000.
        normalised = [(option, option["text"].replace(",", "")) for option in snapshot["options"]]
        for amount in DENOMINATIONS:
            amount_pattern = re.compile(rf"(?<!\d){amount}(?!\d)")
            option = next((opt for opt, text in normalised if amount_pattern.search(text)), None)
            result = None
            detail = "denomination state not exposed by the page"
            if option is not None:
                option_text = option["text"].lower()
                if "out of stock" in option_text or "sold out" in option_text:
                    result, detail = False, "denomination is marked out of stock"
                elif option["disabled"]:
                    detail = "denomination is exposed but not selectable"
                elif option["selected"] and snapshot["addEnabled"]:
                    result, detail = True, "exposed denomination selected and Add to Cart enabled"
                else:
                    result, detail = None, "denomination is exposed but an enabled purchase state is not confirmed"
            elif snapshot["options"]:
                result, detail = False, "requested denomination is not among the exposed options"
            elif snapshot["denominationSectionPresent"] and snapshot["optionContainerPresent"]:
                result, detail = False, "denomination options are absent in the sold-out product state"
            await self.record(f"Woohoo:{amount}", "Woohoo", amount, WOOHOO_URL, result, detail)

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

    async def check_krgkart(self):
        try:
            await self.reset_krg_if_disconnected()
            if self.krg_browser is None:
                if asyncio.get_running_loop().time() < self.krg_retry_after:
                    await self.record_all("KRGKart", KRGKART_DENOMINATIONS, KRGKART_URL, None,
                                          "browser launch retry backoff")
                    return
                await self._start_krg_browser()
            if self.krg_page is None or self.krg_page.is_closed():
                self.krg_page = await self.krg_context.new_page()
                self._configure_page(self.krg_page)
            page = self.krg_page
            response = await page.goto(KRGKART_URL, wait_until="domcontentloaded",
                                       timeout=NAVIGATION_TIMEOUT_MS)
            if response is not None and response.status >= 500:
                # Transient or filter-triggered server error: wait briefly and retry once.
                await page.wait_for_timeout(3000)
                response = await page.goto(KRGKART_URL, wait_until="domcontentloaded",
                                           timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(1200)
            if response is not None and response.status >= 400:
                try:
                    body = (await response.text())[:300].replace("\n", " ")
                except Exception:
                    body = "<unreadable>"
                print(f"KRGKart: HTTP {response.status} | server={response.headers.get('server')} "
                      f"| cf-ray={response.headers.get('cf-ray')} | body={body}")
            if response is None or response.status >= 400:
                detail = ("category page returned no HTTP response" if response is None
                          else f"category page returned HTTP {response.status}")
                await self.record_all("KRGKart", KRGKART_DENOMINATIONS, KRGKART_URL, None, detail)
                return
            snapshot = await page.evaluate("""() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = el => !!(el && el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
              const heading = norm(document.querySelector('h1')?.innerText || document.title);
              const securityPage = /just a moment|security check|verify you are human|checking your browser|access denied|cloudflare/i.test(
                heading + ' ' + norm(document.body?.innerText || '').slice(0, 1200));
              const cards = [...document.querySelectorAll('.product')].filter(visible).map(card => {
                const titleEl = card.querySelector('h2, h3, .product-title, .woocommerce-loop-product__title');
                const title = norm(titleEl?.innerText || '');
                const stockElements = [...card.querySelectorAll('.stock, .availability, [class*="stock"]')].filter(visible);
                const stockText = stockElements.map(el => norm(el.innerText)).filter(Boolean).join(' ');
                const controls = [...card.querySelectorAll('a, button, input[type="submit"]')].filter(el => {
                  const label = norm(el.innerText || el.value || el.getAttribute('aria-label') || '');
                  return visible(el) && /^add to cart$/i.test(label);
                });
                return {title, text: norm(card.innerText), stockText, addCount: controls.length,
                  addUsable: controls.length === 1 && !controls[0].disabled &&
                    controls[0].getAttribute('aria-disabled') !== 'true' &&
                    !/disabled/i.test(controls[0].className || '')};
              });
              return {heading, securityPage, cards};
            }""")
        except KRGKartBrowserUnavailable as exc:
            print(f"KRGKart: BROWSER_UNAVAILABLE - {exc}")
            await self.record_all("KRGKart", KRGKART_DENOMINATIONS, KRGKART_URL, None,
                                  "Google Chrome could not be launched")
            return
        except Exception as exc:
            print(f"KRGKart: PAGE_FAILURE - {type(exc).__name__}: {exc}")
            await self.reset_krg_if_disconnected()
            await self.record_all("KRGKart", KRGKART_DENOMINATIONS, KRGKART_URL, None,
                                  f"category page unreachable ({type(exc).__name__})")
            return

        if snapshot["securityPage"] or "playstation" not in snapshot["heading"].lower():
            reason = "security page shown" if snapshot["securityPage"] else "PlayStation category not identified"
            if snapshot["securityPage"]:
                print("KRGKart: SECURITY_CHALLENGE - stock remains UNKNOWN")
            await self.record_all("KRGKart", KRGKART_DENOMINATIONS, KRGKART_URL, None, reason)
            return

        for amount in KRGKART_DENOMINATIONS:
            amount_pattern = re.compile(rf"\brs\.?\s*{amount}\b", re.I)
            matches = [card for card in snapshot["cards"]
                       if "playstation" in card["title"].lower() and amount_pattern.search(card["title"])]
            if len(matches) != 1:
                result, detail = None, "matching PlayStation product card not found or ambiguous"
            else:
                card = matches[0]
                stock_text = card["stockText"].lower()
                card_text = card["text"].lower()
                if re.search(r"\bout of stock\b", stock_text or card_text):
                    result, detail = False, "product card explicitly says Out of Stock"
                elif (re.search(r"\bin stock\b|\bavailable\b", stock_text)
                      and card["addCount"] == 1 and card["addUsable"]):
                    result, detail = True, "product card shows in-stock state and usable Add to cart control"
                else:
                    result, detail = None, "product stock state or Add to cart control is ambiguous"
            await self.record(f"KRGKart:{amount}", "KRGKart", amount, KRGKART_URL, result, detail)

    @staticmethod
    def _krg_headless():
        """Set KRGKART_HEADED=1 to run KRGKart's Chrome with a visible window."""
        return os.getenv("KRGKART_HEADED", "").lower() not in {"1", "true", "yes"}

    @staticmethod
    def _krg_args():
        return CHROMIUM_ARGS + ["--disable-blink-features=AutomationControlled"]

    async def _start_krg_browser(self):
        if self.playwright is None:
            self._defer_krg_retry()
            raise KRGKartBrowserUnavailable("Playwright is unavailable")

        explicit_path = os.getenv("KRGKART_BROWSER_PATH") or os.getenv("KRGKART_CHROME_PATH")
        attempts = []
        selected_label = None
        try:
            if explicit_path:
                attempts.append("explicit executable")
                try:
                    self.krg_browser = await self.playwright.chromium.launch(
                        executable_path=explicit_path, headless=self._krg_headless(),
                        args=self._krg_args()
                    )
                    selected_label = "explicit executable (headless)"
                except Exception:
                    self._defer_krg_retry()
                    raise KRGKartBrowserUnavailable("tried explicit executable only")
            else:
                for channel, label in (("chrome", "chrome (headless)"), ("msedge", "msedge (headless)")):
                    attempts.append(f"channel {channel}")
                    try:
                        self.krg_browser = await self.playwright.chromium.launch(
                            channel=channel, headless=self._krg_headless(),
                            args=self._krg_args()
                        )
                        selected_label = label
                        break
                    except Exception:
                        continue

                if self.krg_browser is None:
                    self._defer_krg_retry()
                    raise KRGKartBrowserUnavailable("tried " + ", ".join(attempts))

            # KRGKart rejects the stock headless fingerprint (HTTP 500), so it gets a
            # realistic browser identity and loads all resources normally.
            self.krg_context = await self.krg_browser.new_context(
                user_agent=KRGKART_USER_AGENT,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            )
            await self.krg_context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            self.krg_page = await self.krg_context.new_page()
            self._configure_page(self.krg_page)
            print(f"KRGKart: using {selected_label}")
        except KRGKartBrowserUnavailable:
            await self.close_krg_browser()
            raise
        except Exception as exc:
            await self.close_krg_browser()
            self._defer_krg_retry()
            raise KRGKartBrowserUnavailable(
                f"browser initialized incompletely; tried {', '.join(attempts) or 'browser setup'} "
                f"({type(exc).__name__})"
            ) from exc

    async def close_krg_browser(self):
        page, context, browser = self.krg_page, self.krg_context, self.krg_browser
        self.krg_page = None
        self.krg_context = None
        self.krg_browser = None
        errors = []
        for name, resource in (("page", page), ("context", context), ("browser", browser)):
            if resource is None:
                continue
            try:
                if name == "page" and resource.is_closed():
                    continue
                if name == "browser" and not resource.is_connected():
                    continue
                await resource.close()
            except Exception:
                errors.append(name)
        return errors

    async def reset_krg_if_disconnected(self):
        if self.krg_browser is not None and not self.krg_browser_is_connected():
            if await self.close_krg_browser():
                print("⚠️ KRGKart: browser cleanup warning after disconnect")
            self._defer_krg_retry()

    def krg_browser_is_connected(self):
        if self.krg_browser is None:
            return False
        try:
            return self.krg_browser.is_connected()
        except Exception:
            return False

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
            ("Woohoo", self.check_woohoo, [f"Woohoo:{n}" for n in DENOMINATIONS]),
            ("KRGKart", self.check_krgkart, [f"KRGKart:{n}" for n in KRGKART_DENOMINATIONS]),
        ]
        checks.extend((f"GamesTheShop ₹{amount}", lambda a=amount, u=url: self.check_gamestheshop(a, u),
                       [f"GamesTheShop:{amount}"])
                      for amount, url in GAMES_THE_SHOP.items())
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
            for name, close in (("KRGKart", monitor.close_krg_browser),
                                ("Browser context", monitor.browser_context.close),
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
