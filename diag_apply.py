"""Диагностика: что появляется после клика «Откликнуться» на вакансии."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

VACANCY_ID = "135838069"
USER_DATA_DIR = Path("browser_data")
OUT = Path("debug_screenshots") / f"diag_apply_{VACANCY_ID}.txt"


def dump(page, lines):
    lines.append("=" * 60)
    lines.append(f"URL: {page.url}")
    lines.append("=" * 60)

    lines.append("--- кнопки (visible) ---")
    for b in page.locator("button").all():
        try:
            if not b.is_visible():
                continue
            qa = b.get_attribute("data-qa") or ""
            text = (b.text_content() or "").strip()[:80]
            lines.append(f"btn data-qa={qa!r} text={text!r}")
        except Exception:
            pass

    lines.append("--- элементы data-qa с response/popup/letter/confirm ---")
    for sel in [
        "[data-qa*='response']",
        "[data-qa*='popup']",
        "[data-qa*='letter']",
        "[data-qa*='confirm']",
        "[class*='modal']",
        "[class*='popup']",
    ]:
        try:
            for el in page.locator(sel).all():
                if not el.is_visible():
                    continue
                qa = el.get_attribute("data-qa") or ""
                text = (el.text_content() or "").strip().replace("\n", " ")[:150]
                lines.append(f"{sel} data-qa={qa!r} text={text!r}")
        except Exception as e:
            lines.append(f"{sel} ERROR {e}")

    lines.append("--- body text (первые 3000) ---")
    body = (page.locator("body").text_content() or "").strip()
    lines.append(body[:3000])


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        url = f"https://hh.ru/vacancy/{VACANCY_ID}"
        lines = []
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(5)
            lines.append("STATE: до клика")
            dump(page, lines)

            selectors = [
                "button[data-qa='vacancy-response-response__open']",
                "a[data-qa='vacancy-response-response__open']",
                "button:has-text('Откликнуться')",
                "a:has-text('Откликнуться')",
            ]
            clicked = False
            for sel in selectors:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    lines.append(f"CLICK: {sel}")
                    loc.first.click()
                    clicked = True
                    break
            if not clicked:
                lines.append("CLICK: кнопка не найдена")

            time.sleep(6)
            lines.append("\nSTATE: после клика (6s)")
            dump(page, lines)
            time.sleep(3)
            lines.append("\nSTATE: после клика (9s)")
            dump(page, lines)
        except Exception as e:
            lines.append(f"ERROR: {type(e).__name__}: {e}")
        finally:
            OUT.write_text("\n".join(lines), encoding="utf-8")
            try:
                page.screenshot(path=str(Path("debug_screenshots") / f"diag_apply_{VACANCY_ID}.png"))
            except Exception:
                pass
            ctx.close()
    print("saved to", OUT)


if __name__ == "__main__":
    main()
