"""
Recon script: load the Momence page and extract slot structure.
Run with: venv/bin/python scripts/recon.py
Outputs:
  - scripts/recon_page.html   (full rendered HTML)
  - scripts/recon_network.json  (any API responses that look like slot data)
  - prints a structured summary of what slots look like
"""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://momence.com/u/fjord-kaXq4Q"
OUT_DIR = Path(__file__).parent

api_responses = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # Intercept network responses that look like slot/schedule data
        async def handle_response(response):
            url = response.url
            if any(k in url for k in ["session", "schedule", "event", "booking", "slot", "availability", "offering", "class"]):
                try:
                    body = await response.json()
                    api_responses.append({"url": url, "status": response.status, "body": body})
                    print(f"  [network] captured: {url} ({response.status})")
                except Exception:
                    pass  # not JSON or unreadable

        page.on("response", handle_response)

        print(f"Loading {URL} ...")
        await page.goto(URL, wait_until="networkidle", timeout=30000)

        # Extra wait for any lazy-rendered content
        await asyncio.sleep(4)

        # Scroll down to trigger any lazy loading
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        # --- Save full rendered HTML ---
        html = await page.content()
        html_path = OUT_DIR / "recon_page.html"
        html_path.write_text(html)
        print(f"\nSaved rendered HTML ({len(html):,} chars) → {html_path}")

        # --- Save captured network responses ---
        if api_responses:
            net_path = OUT_DIR / "recon_network.json"
            net_path.write_text(json.dumps(api_responses, indent=2, default=str))
            print(f"Saved {len(api_responses)} API response(s) → {net_path}")
        else:
            print("No matching API responses intercepted.")

        # --- Try to extract slot elements from DOM ---
        print("\n--- DOM Slot Extraction ---")

        # Broad sweep: look for any elements that contain time-like patterns
        time_elements = await page.locator("text=/\\d{1,2}:\\d{2}\\s*(AM|PM|am|pm)/").all()
        print(f"Elements containing time strings: {len(time_elements)}")
        for el in time_elements[:10]:
            try:
                text = (await el.text_content() or "").strip()
                tag = await el.evaluate("el => el.tagName")
                classes = await el.evaluate("el => el.className")
                print(f"  <{tag} class='{classes}'> {repr(text[:120])}")
            except Exception:
                pass

        # Look for date-like patterns
        date_elements = await page.locator("text=/(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)/").all()
        print(f"\nElements containing day-of-week strings: {len(date_elements)}")
        for el in date_elements[:5]:
            try:
                text = (await el.text_content() or "").strip()
                tag = await el.evaluate("el => el.tagName")
                classes = await el.evaluate("el => el.className")
                print(f"  <{tag} class='{classes}'> {repr(text[:120])}")
            except Exception:
                pass

        # Dump first slot-like card's outer HTML for structure inspection
        print("\n--- First card-like element outer HTML ---")
        card_candidates = [
            "[class*='card']",
            "[class*='session']",
            "[class*='slot']",
            "[class*='event']",
            "[class*='class']",
            "[class*='booking']",
            "[class*='schedule']",
            "[class*='offering']",
        ]
        for selector in card_candidates:
            els = await page.locator(selector).all()
            if els:
                html_snip = await els[0].evaluate("el => el.outerHTML")
                print(f"\nSelector '{selector}' matched {len(els)} element(s). First:")
                print(html_snip[:2000])
                break

        # --- Print page title and any visible text summary ---
        title = await page.title()
        print(f"\nPage title: {title}")

        # Grab all visible text, collapsed
        body_text = await page.evaluate("document.body.innerText")
        lines = [l.strip() for l in body_text.splitlines() if l.strip()]
        print(f"\nVisible text ({len(lines)} lines). First 60:")
        for line in lines[:60]:
            print(f"  {line}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
