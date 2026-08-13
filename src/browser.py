import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from random import uniform
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright._impl._errors import TargetClosedError, Error as PlaywrightError
from .storage import (
    get_connection, has_applied, get_evaluation,
    mark_applied, mark_failed, mark_evaluated, count_today,
)
from .llm import DeepSeekClient, OllamaMatcher

log = logging.getLogger("offrers.browser")

USER_DATA_DIR = Path("browser_data")
HH_BASE = "https://hh.ru"
DEBUG_DIR = Path("debug_screenshots")


def _screenshot(page: Page, name: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log.info("[debug] Screenshot saved: %s", path)


def _build_matcher(config):
    m = config.match
    if not m.enabled:
        log.info("match.enabled=false -> матчинг отключён")
        return None
    if not config.apply.resume_text:
        log.warning("match.enabled=true, но apply.resume_text пуст — матчинг отключён")
        return None
    log.info("Матчинг включён: model=%s, url=%s, threshold=%s", m.model, m.model_url, m.threshold)
    return OllamaMatcher(
        model_url=m.model_url,
        model=m.model,
        threshold=m.threshold,
        max_chars=m.max_chars,
        timeout=m.timeout,
    )


def _build_query_gen(config):
    if not (config.search.auto_queries and config.apply.resume_text):
        if config.search.auto_queries:
            log.warning("search.auto_queries=true, но apply.resume_text пуст — генерация запросов отключена")
        return None
    m = config.match
    log.info("Генерация запросов включена: model=%s", m.model)
    return OllamaMatcher(
        model_url=m.model_url,
        model=m.model,
        threshold=m.threshold,
        max_chars=m.max_chars,
        timeout=m.timeout,
    )


def _extract_vacancy_text(page: Page) -> str:
    def grab(selector: str, timeout: int = 2000) -> str:
        try:
            loc = page.locator(selector).first
            return (loc.text_content(timeout=timeout) or "").strip()
        except Exception:
            return ""

    title = grab("h1[data-qa='vacancy-title']") or grab("h1")
    company = grab("[data-qa='vacancy-company-name']")

    desc = ""
    for sel in ("[data-qa='vacancy-description']", "[class*='description']", "article"):
        if text := grab(sel):
            desc = text
            break
    if len(desc) > 5000:
        desc = desc[:5000] + "..."

    skills = ""
    try:
        for sel in (
            "[data-qa='skills-element']",
            "[data-qa='bloko-tag']",
            "[data-qa='vacancy-key-skills'] [data-qa='skills-element']",
        ):
            tags = [t.strip() for t in page.locator(sel).all_text_contents() if t.strip()]
            if tags:
                skills = ", ".join(tags[:20])
                break
    except Exception:
        pass

    log.debug("vacancy extracted: title=%r, company=%r, desc_len=%d, skills=%r",
              title, company, len(desc), skills[:80])
    parts = [title]
    if company:
        parts.append(f"Компания: {company}")
    if desc:
        parts.append(f"Описание: {desc}")
    if skills:
        parts.append(f"Навыки: {skills}")
    return "\n".join(parts)


def _build_search_url(config, text: str | None = None) -> str:
    from urllib.parse import urlencode

    params = {}
    if text:
        params["text"] = text
    elif config.search.text:
        params["text"] = config.search.text
    for a in config.search.area:
        params.setdefault("area", []).append(a)
    if config.search.salary_from:
        params["salary"] = config.search.salary_from
        params["only_with_salary"] = "true"
    if config.search.employment:
        params["employment"] = config.search.employment
    if config.search.schedule:
        params["schedule"] = config.search.schedule
    if config.search.experience:
        params["experience"] = config.search.experience
    if config.search.remote:
        params["schedule"] = "remote"
    params["order_by"] = "publication_time"
    params["per_page"] = 50
    url = f"{HH_BASE}/search/vacancy?{urlencode(params, doseq=True)}"
    log.debug("search url built: %s", url)
    return url


def _with_page(url: str, page: int) -> str:
    """Добавляет параметр page к URL поиска. 2-я страница = page=1 (0-индексация hh.ru)."""
    from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

    parts = urlparse(url)
    params = parse_qs(parts.query, keep_blank_values=True)
    params["page"] = [str(page)]
    query = urlencode(params, doseq=True)
    return urlunparse(parts._replace(query=query))


def _wait_for_captcha(page: Page):
    try:
        if page.locator("input[data-ti='captcha']").count() > 0:
            print("\n[!] CAPTCHA. Solve it and press Enter...")
            log.warning("CAPTCHA detected (input[data-ti='captcha']) — ожидаем ввода")
            input()
            page.wait_for_load_state("domcontentloaded")
            log.info("CAPTCHA решена, продолжаем")
            return
        if page.locator("img[src*='captcha']").count() > 0:
            print("\n[!] CAPTCHA. Solve it and press Enter...")
            log.warning("CAPTCHA detected (img[src*='captcha']) — ожидаем ввода")
            input()
            page.wait_for_load_state("domcontentloaded")
            log.info("CAPTCHA решена, продолжаем")
    except Exception:
        pass


def _find_apply_button(page: Page):
    selectors = [
        "button[data-qa='vacancy-response-response__open']",
        "a[data-qa='vacancy-response-response__open']",
        "button:has-text('Откликнуться')",
        "a:has-text('Откликнуться')",
    ]
    for sel in selectors:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible():
            log.debug("apply button found: selector=%s", sel)
            return btn.first
    log.debug("apply button NOT found (tried %d selectors)", len(selectors))
    return None


def _handle_city_modal(page: Page):
    time.sleep(uniform(1.0, 2.0))
    confirm_selectors = [
        "button[data-qa='vacancy-response-confirm-submit']",
        "button:has-text('Всё равно откликнуться')",
        "button:has-text('Все равно откликнуться')",
        "button:has-text('Откликнуться всё равно')",
        "button:has-text('Откликнуться все равно')",
    ]
    for sel in confirm_selectors:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible():
            log.debug("city mismatch modal confirmed: selector=%s", sel)
            btn.first.click()
            time.sleep(uniform(1.5, 3.0))
            _wait_for_captcha(page)
            return True
    log.debug("no city mismatch modal detected")
    return False


def _select_resume(page: Page) -> bool:
    """Выбирает резюме в модалке отклика. Возвращает True, если выбрано."""
    sel = page.locator("select[data-qa='vacancy-response-select-resume']")
    if sel.count() == 0:
        sel = page.locator("[data-qa='resume-selector'] select")
    if sel.count() > 0:
        opts = sel.first.locator("option")
        for i in range(opts.count()):
            value = opts.nth(i).get_attribute("value") or ""
            if not value:
                continue
            try:
                sel.first.select_option(value=value)
                log.debug("resume selected via <select> (%s)", value)
                time.sleep(uniform(0.5, 1.5))
                return True
            except Exception as e:
                log.warning("не удалось выбрать резюме через select: %s", e)
                break

    for sels in (
        "[data-qa='resume-item']",
        "[data-qa='resume-selector'] [data-qa='resume-card']",
        "[class*='resume'] [class*='card']",
    ):
        cards = page.locator(sels)
        if cards.count() > 0:
            try:
                cards.first.click()
                log.debug("resume card selected (%s, count=%d)", sels, cards.count())
                time.sleep(uniform(0.5, 1.0))
                return True
            except Exception as e:
                log.warning("не удалось кликнуть по карточке резюме: %s", e)
                return False

    log.debug("resume selector not found in modal")
    return False


def _handle_apply_modal(page: Page, config, cover_letter: str = "") -> dict:
    log.debug("waiting for apply modal...")
    time.sleep(uniform(3.0, 5.0))
    _wait_for_captcha(page)

    current_url = page.url
    log.debug("apply modal check, url=%s", current_url)
    if "/resume" in current_url:
        log.info("redirected to /resume — требуется действие")
        return {"status": "requires_action", "reason": "redirected_to_resume"}

    success_selectors = [
        "[data-qa='responded-success']",
        "[data-qa*='responded-success']",
        "button[data-qa*='responded-success-attach-cover-letter']",
    ]
    for sel in success_selectors:
        if page.locator(sel).count() > 0:
            log.info("панель успеха (responded-success) — отклик принят: selector=%s", sel)
            return {"status": "applied"}

    page_text_now = page.locator("body").text_content() or ""
    if "Вы откликнулись" in page_text_now and "Откликнуться" not in page_text_now:
        log.info("в тексте страницы 'Вы откликнулись' без кнопки отклика — отклик принят")
        return {"status": "applied"}

    modal_selectors = [
        "button[data-qa='vacancy-response-submit-popup']",
        "button[data-qa='vacancy-response-response__submit']",
        "button[data-qa='vacancy-response-letter-submit']",
        "div[data-qa='vacancy-response-popup'] button[type='submit']",
        "div[class*='modal'] button[type='submit']",
        "div[class*='response'] button[type='submit']",
        "button:has-text('Отправить отклик')",
        "button:has-text('Откликнуться')",
    ]

    submit_btn = None
    for sel in modal_selectors:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible():
            log.debug("submit button found: selector=%s", sel)
            submit_btn = btn.first
            break

    if submit_btn is None:
        log.debug("submit button not found, checking page text...")
        page_text = page.locator("body").text_content() or ""
        if "Вы уже откликались" in page_text or "уже откликались" in page_text or "Вы откликнулись" in page_text:
            log.info("already applied (page text)")
            return {"status": "already_applied"}
        if "Тестовое задание" in page_text or "тестовое задание" in page_text:
            log.info("тестовое задание — пропуск")
            return {"status": "requires_action", "reason": "test_required"}
        log.warning("apply modal not found, no known marker in page text")
        return {"status": "no_modal"}

    time.sleep(uniform(1.0, 2.0))

    radio_groups = page.locator("[data-qa='vacancy-response-question'] [role='radiogroup']")
    if radio_groups.count() == 0:
        radio_groups = page.locator("[class*='question'] [role='radiogroup']")
    if radio_groups.count() > 0:
        log.info("в форме вопросы с вариантами (%d) — пропуск", radio_groups.count())
        return {"status": "requires_action", "reason": "questions"}

    text_questions = page.locator("[data-qa='vacancy-response-question'] textarea")
    if text_questions.count() == 0:
        text_questions = page.locator("[class*='question'] textarea")
    if text_questions.count() > 0:
        log.info("текстовые вопросы (%d) — пропуск", text_questions.count())
        return {"status": "requires_action", "reason": "text_questions"}

    resume_selected = _select_resume(page)

    msg_field = page.locator("textarea[data-qa='vacancy-response-letter']")
    if msg_field.count() == 0:
        msg_field = page.locator("[data-qa='vacancy-response-letter'] textarea")
    if msg_field.count() == 0:
        msg_field = page.locator("textarea[data-qa='vacancy-response-response__input']")
    if msg_field.count() == 0:
        msg_field = page.locator("textarea[placeholder*='сопроводительн']")
    if msg_field.count() == 0:
        msg_field = page.locator("textarea[placeholder*='Сопроводительн']")
    # поле письма появляется только после выбора резюме — ждём его
    if msg_field.count() == 0 and resume_selected:
        for _ in range(8):
            time.sleep(0.75)
            for sel in (
                "textarea[data-qa='vacancy-response-letter']",
                "[data-qa='vacancy-response-letter'] textarea",
            ):
                msg_field = page.locator(sel)
                if msg_field.count() > 0:
                    break
            if msg_field.count() > 0:
                break
    # общий fallback: первый видимый textarea вне блоков вопросов
    if msg_field.count() == 0:
        for ta in page.locator("textarea").all():
            try:
                if not ta.is_visible():
                    continue
                if ta.evaluate(
                    "el => !!el.closest('[data-qa*=\"question\"], [data-qa*=\"Question\"], [class*=\"question\"]')"
                ):
                    continue
                msg_field = ta
                log.debug("cover letter field via generic textarea fallback")
                break
            except Exception:
                continue
    if msg_field.count() > 0:
        log.debug("cover letter field found, len(cover_letter)=%d", len(cover_letter))
        if cover_letter:
            msg_field.first.fill(cover_letter)
            time.sleep(uniform(0.5, 1.0))
        else:
            log.info("сопроводительное письмо требуется, но текста нет — пропуск")
            return {"status": "requires_action", "reason": "cover_letter"}
    else:
        log.debug("no cover letter field in modal")

    for attempt in range(12):
        if submit_btn.first.is_enabled():
            log.debug("submit button enabled (attempt %d)", attempt + 1)
            break
        log.debug("submit button still disabled (attempt %d/12)", attempt + 1)
        time.sleep(uniform(0.5, 1.0))

    if not submit_btn.first.is_enabled():
        log.warning("submit button stays disabled after 12 attempts")
        return {"status": "button_disabled"}

    log.debug("clicking submit")
    submit_btn.first.click()
    time.sleep(uniform(1.5, 3.0))

    if page.locator("[data-qa='vacancy-response-error']").count() > 0:
        text = page.locator("[data-qa='vacancy-response-error']").text_content()
        log.warning("response error element: %s", text)
        return {"status": "error", "text": text}

    already = page.locator("span[data-qa='vacancy-response-already-invited']")
    if already.count() == 0:
        already = page.locator(":has-text('Вы уже откликались')")
    if already.count() > 0:
        log.info("already applied (post-submit marker)")
        return {"status": "already_applied"}

    log.info("submit OK — отклик отправлен")
    return {"status": "applied"}


def login(context: BrowserContext):
    page = context.new_page()
    log.info("открываю %s для логина", HH_BASE)
    page.goto(f"{HH_BASE}", wait_until="domcontentloaded")
    _wait_for_captcha(page)

    print("If you are not logged in — log in now.")
    print("After logging in, press Enter in the terminal...")
    input()

    log.info("session saved (профиль %s)", USER_DATA_DIR)
    page.close()


def _apply_single(vp, vacancy_id, config, conn, stats, stats_lock, dry_run, llm, vacancy_text=""):
    """Выполняет отклик на странице вакансии vp. Обновляет stats."""
    log.info("[%s] проверка капчи/ожидание", vacancy_id)
    _wait_for_captcha(vp)
    time.sleep(uniform(2.0, 4.0))

    apply_btn = _find_apply_button(vp)
    if not apply_btn:
        page_text = vp.locator("body").text_content() or ""
        if "Вы откликнулись" in page_text or "уже откликались" in page_text:
            mark_applied(conn, vacancy_id)
            with stats_lock:
                stats["skipped"] += 1
            log.info("[%s] уже откликались ранее — помечаю (без кнопки)", vacancy_id)
            return
        log.warning("[%s] кнопка 'Откликнуться' НЕ найдена — пропуск", vacancy_id)
        _screenshot(vp, f"no_button_{vacancy_id}")
        mark_failed(conn, vacancy_id, "no_apply_button")
        with stats_lock:
            stats["errors"] += 1
        return

    if dry_run:
        log.info("[%s] dry-run: кнопка найдена, отклик не выполняю", vacancy_id)
        mark_applied(conn, vacancy_id)
        with stats_lock:
            stats["applied"] += 1
        return

    log.info("[%s] кликаю 'Откликнуться'", vacancy_id)
    apply_btn.click()
    time.sleep(uniform(1.0, 2.0))
    _wait_for_captcha(vp)

    _handle_city_modal(vp)

    cover_letter = ""
    if llm:
        try:
            if not vacancy_text:
                vacancy_text = _extract_vacancy_text(vp)
            log.debug("[%s] генерирую сопроводительное письмо (LLM)...", vacancy_id)
            cover_letter = llm.generate_cover_letter(vacancy_text, llm.resume_text)
            log.info("[%s] письмо сгенерировано (%d символов)", vacancy_id, len(cover_letter))
        except Exception as e:
            log.warning("[%s] ошибка LLM: %s", vacancy_id, e)
    if not cover_letter:
        cover_letter = config.apply.cover_letter or config.apply.message
        log.debug("[%s] письмо из конфига (%d символов)", vacancy_id, len(cover_letter))

    result = _handle_apply_modal(vp, config, cover_letter=cover_letter)
    log.debug("[%s] результат модалки: %s", vacancy_id, result)

    if result["status"] == "applied":
        mark_applied(conn, vacancy_id)
        with stats_lock:
            stats["applied"] += 1
        log.info("[%s] [+] отклик отправлен успешно", vacancy_id)
    elif result["status"] == "already_applied":
        mark_applied(conn, vacancy_id)
        with stats_lock:
            stats["skipped"] += 1
        log.info("[%s] уже откликались ранее — помечаю", vacancy_id)
    elif result["status"] == "requires_action":
        log.warning("[%s] пропуск: %s", vacancy_id, result.get("reason", "requires action"))
        mark_failed(conn, vacancy_id, result.get("reason", "requires_action"))
        with stats_lock:
            stats["skipped"] += 1
    elif result["status"] == "button_disabled":
        log.error("[%s] кнопка отправки неактивна — форма не готова", vacancy_id)
        _screenshot(vp, f"disabled_btn_{vacancy_id}")
        mark_failed(conn, vacancy_id, "button_disabled")
        with stats_lock:
            stats["errors"] += 1
    elif result["status"] == "error":
        log.error("[%s] ошибка формы: %s", vacancy_id, result.get("text", "?"))
        mark_failed(conn, vacancy_id, "response_error")
        with stats_lock:
            stats["errors"] += 1
    else:
        log.error("[%s] не удалось откликнуться (%s)", vacancy_id, result["status"])
        _screenshot(vp, f"apply_failed_{vacancy_id}")
        mark_failed(conn, vacancy_id, result["status"])
        with stats_lock:
            stats["errors"] += 1


def search_and_apply_browser(context: BrowserContext, config, dry_run: bool = False) -> dict:
    stats = {"found": 0, "applied": 0, "skipped": 0, "errors": 0, "not_evaluated": 0}
    stats_lock = threading.Lock()
    db_lock = threading.Lock()
    conn = get_connection()

    llm = None
    ds = config.deepseek
    if ds.enabled and ds.api_key and config.apply.resume_text:
        try:
            log.info("DeepSeek включён: model=%s, language=%s", ds.model, ds.language)
            llm = DeepSeekClient(
                api_key=ds.api_key,
                base_url=ds.base_url,
                model=ds.model,
                language=ds.language,
                max_tokens=ds.max_tokens,
                temperature=ds.temperature,
            )
            llm.resume_text = config.apply.resume_text
        except Exception as e:
            log.warning("не удалось инициализировать LLM: %s", e)
    elif ds.enabled:
        log.warning("deepseek.enabled=true, но не хватает api_key или resume_text — письма не генерируются")

    matcher = _build_matcher(config)
    if llm is None and matcher is not None and config.apply.resume_text:
        log.info("DeepSeek выключен — сопроводительные письма генерирую через Ollama (%s)", matcher.model)
        llm = matcher
        llm.resume_text = config.apply.resume_text
    qgen = _build_query_gen(config)
    if qgen is None and matcher is not None and config.search.auto_queries:
        qgen = matcher

    analyze_q = queue.Queue()
    apply_q = queue.Queue()
    batch_size = max(1, int(config.apply.batch_size or 10))
    max_pages = max(1, int(config.search.max_pages or 20))

    class _DailyLimitReached(Exception):
        pass

    last_apply = 0.0
    page = None
    stop_event = threading.Event()
    executor = None
    worker_futures = []

    def apply_batch() -> None:
        """Откликается по батчу из apply_q. Вызывает _DailyLimitReached при лимите."""
        nonlocal last_apply
        n = 0
        while n < batch_size and not apply_q.empty():
            vacancy_id, vacancy_url, vacancy_text = apply_q.get()
            log.debug("apply batch: осталось в очереди %d", apply_q.qsize())

            with db_lock:
                already = has_applied(conn, vacancy_id)
            if already:
                log.debug("%s уже в applied — пропуск", vacancy_id)
                with stats_lock:
                    stats["skipped"] += 1
                continue

            with db_lock:
                today_count = count_today(conn)
            if today_count >= config.apply.max_per_day:
                log.warning("достигнут дневной лимит (%d) — остановка", config.apply.max_per_day)
                raise _DailyLimitReached()

            if last_apply and (time.time() - last_apply) < 5:
                wait = uniform(2.0, 5.0)
                log.info("пауза %.1fс перед откликом (минимум 5с)", wait)
                time.sleep(wait)

            log.info("открываю %s", vacancy_url)
            vp = context.new_page()
            try:
                try:
                    vp.goto(vacancy_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    log.error("не удалось перейти на %s: %s", vacancy_url, e)
                    mark_failed(conn, vacancy_id, "nav_error")
                    with stats_lock:
                        stats["errors"] += 1
                    continue
                _apply_single(vp, vacancy_id, config, conn, stats, stats_lock, dry_run, llm, vacancy_text)
            finally:
                try:
                    vp.close()
                except Exception:
                    pass
            last_apply = time.time()
            n += 1

    def match_worker() -> None:
        max_retries = max(0, int(config.match.max_retries or 0))
        while True:
            try:
                item = analyze_q.get(timeout=0.5)
            except queue.Empty:
                if stop_event.is_set():
                    return
                continue
            vid, url, text, retries = item
            try:
                log.debug("матчинг %s...", vid)
                ok, score, reason = matcher.is_match(text, config.apply.resume_text)
                if ok:
                    with db_lock:
                        mark_evaluated(conn, vid, "match", score, reason)
                    apply_q.put((vid, url, text))
                    log.info("подходит %s (score=%s): %s", vid, score, reason)
                else:
                    with db_lock:
                        mark_evaluated(conn, vid, "skip", score, reason)
                    with stats_lock:
                        stats["skipped"] += 1
                    log.info("не подходит %s (score=%s): %s", vid, score, reason)
            except Exception as e:
                if retries < max_retries:
                    analyze_q.put((vid, url, text, retries + 1))
                    log.warning("матчинг %s: %s — повторная попытка %d/%d", vid, e, retries + 1, max_retries)
                else:
                    log.warning("матчинг %s: %s — попытки исчерпаны, пропуск", vid, e)
                    with stats_lock:
                        stats["not_evaluated"] += 1

    def collect_query(query_url) -> int:
        nonlocal last_apply
        log.info("search page: %s", query_url)
        total_new = 0
        try:
            page.goto(query_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            log.error("не удалось загрузить страницу поиска: %s", e)
            return 0
        _wait_for_captcha(page)
        time.sleep(4)

        for current_page in range(max_pages):
            vacancy_links = page.locator("a[data-qa='serp-item__title']").all()
            if not vacancy_links:
                vacancy_links = page.locator("[data-qa='serp-item'] a[href*='/vacancy/']").all()
            if not vacancy_links:
                log.info("на странице %d нет вакансий", current_page + 1)
                return total_new

            hrefs = []
            for link in vacancy_links:
                try:
                    href = link.get_attribute("href", timeout=3000) or ""
                except Exception:
                    continue
                if "/vacancy/" not in href:
                    continue
                vacancy_id = href.split("/vacancy/")[1].split("?")[0].split("/")[0]
                hrefs.append((vacancy_id, f"{HH_BASE}/vacancy/{vacancy_id}"))
            log.debug("страница %d: найдено ссылок=%d", current_page + 1, len(hrefs))

            new_ids = 0
            for vacancy_id, vacancy_url in hrefs:
                if vacancy_id in seen:
                    continue
                seen.add(vacancy_id)
                new_ids += 1
                total_new += 1

                with db_lock:
                    already = has_applied(conn, vacancy_id)
                if already:
                    log.debug("%s уже в applied — пропуск", vacancy_id)
                    with stats_lock:
                        stats["skipped"] += 1
                    continue
                with db_lock:
                    ev = get_evaluation(conn, vacancy_id)
                if ev and ev["status"] == "skip":
                    log.debug("%s уже оценён как skip — пропуск", vacancy_id)
                    with stats_lock:
                        stats["skipped"] += 1
                    continue
                if ev and ev["status"] == "failed":
                    log.debug("%s уже в failed — пропуск", vacancy_id)
                    with stats_lock:
                        stats["skipped"] += 1
                    continue

                backlog = max(1, int(config.match.backlog or 10))
                resume_at = max(1, backlog // 2)
                if matcher and analyze_q.qsize() >= backlog:
                    while analyze_q.qsize() >= resume_at:
                        log.info("очередь матчинга %d — пауза сбора до %d (Ollama не успевает)",
                                 analyze_q.qsize(), resume_at)
                        time.sleep(2)

                log.debug("сбор вакансии %s", vacancy_url)
                vp = context.new_page()
                try:
                    try:
                        vp.goto(vacancy_url, wait_until="domcontentloaded", timeout=15000)
                        _wait_for_captcha(vp)
                        time.sleep(uniform(2.0, 4.0))
                        vacancy_text = _extract_vacancy_text(vp)
                    except Exception as e:
                        log.warning("не удалось обработать %s: %s", vacancy_id, e)
                        with stats_lock:
                            stats["errors"] += 1
                        continue
                finally:
                    try:
                        vp.close()
                    except Exception:
                        pass

                if ev and ev["status"] == "match":
                    log.debug("%s уже оценён как match — в очередь отклика", vacancy_id)
                    apply_q.put((vacancy_id, vacancy_url, vacancy_text))
                elif matcher:
                    analyze_q.put((vacancy_id, vacancy_url, vacancy_text, 0))
                else:
                    apply_q.put((vacancy_id, vacancy_url, vacancy_text))

            with stats_lock:
                stats["found"] += new_ids

            # отклик сразу, как только в очереди набрался батч (и не останавливаем сбор полностью)
            if not apply_q.empty() and apply_q.qsize() >= batch_size:
                log.info("в очереди отклика >= %d — прерываю сбор и выполняю отклик", batch_size)
                apply_batch()

            if current_page + 1 >= max_pages:
                log.info("достигнут лимит страниц (%d) — переходим к следующему запросу", max_pages)
                return total_new
            if new_ids == 0:
                log.info("страница %d целиком из дублей — переходим к следующему запросу", current_page + 1)
                return total_new

            log.info("следующая страница (%d)...", current_page + 2)
            page.goto(_with_page(query_url, current_page + 1), wait_until="domcontentloaded", timeout=15000)
            _wait_for_captcha(page)
            time.sleep(uniform(2.0, 4.0))

        return total_new

    try:
        seen = set()
        page = context.new_page()

        queries = []
        if config.search.text:
            queries.append(config.search.text)
        if qgen:
            try:
                log.info("генерирую поисковые запросы из резюме (max=%d)...", config.search.max_queries)
                gen = qgen.generate_queries(config.apply.resume_text, config.search.max_queries)
                if gen:
                    for q in gen:
                        if q and q not in queries:
                            queries.append(q)
                    log.info("итоговые запросы: %s", queries)
            except Exception as e:
                log.warning("ошибка генерации запросов: %s", e)
        if not queries:
            queries = [""]
        log.info("PHASE A: сбор кандидатов, запросов в очереди: %d", len(queries))

        # матчинг-воркеры стартуют сразу и работают параллельно со сбором
        if matcher:
            workers = max(1, int(config.match.workers or 4))
            log.info("матчинг включён: %d воркеров работают параллельно со сбором", workers)
            executor = ThreadPoolExecutor(max_workers=workers)
            worker_futures = [executor.submit(match_worker) for _ in range(workers)]

        max_rounds = max(1, int(getattr(config.search, "max_rounds", 1) or 1))
        rd = getattr(config.search, "round_delay_seconds", None) or {"min": 60, "max": 180}
        rd_min = max(0.0, float(rd.get("min", 60)))
        rd_max = max(rd_min, float(rd.get("max", 180)))

        for round_no in range(1, max_rounds + 1):
            with db_lock:
                today_count = count_today(conn)
            if today_count >= config.apply.max_per_day:
                log.warning("достигнут дневной лимит (%d) — остановка", config.apply.max_per_day)
                break

            log.info("=== РАУНД %d/%d (откликов сегодня: %d/%d) ===",
                     round_no, max_rounds, today_count, config.apply.max_per_day)

            round_new = 0
            for q in queries:
                round_new += collect_query(_build_search_url(config, q))
                # после каждого запроса тоже сбрасываем накопленную очередь
                if not apply_q.empty():
                    log.info("сброс очереди отклика после запроса (в очереди %d)", apply_q.qsize())
                    apply_batch()
                with db_lock:
                    if count_today(conn) >= config.apply.max_per_day:
                        break

            if not apply_q.empty():
                log.info("сброс очереди отклика после раунда (в очереди %d)", apply_q.qsize())
                apply_batch()

            with db_lock:
                today_count = count_today(conn)
            if today_count >= config.apply.max_per_day:
                log.warning("достигнут дневной лимит (%d) — остановка", config.apply.max_per_day)
                break

            if round_new == 0:
                log.info("раунд %d не дал новых вакансий — завершаю (свежих объявлений нет)", round_no)
                break

            if round_no < max_rounds:
                pause = uniform(rd_min, rd_max)
                log.info("пауза между раундами %.1fс (откликов сегодня: %d/%d)...",
                         pause, today_count, config.apply.max_per_day)
                time.sleep(pause)

        try:
            page.close()
        except Exception:
            pass
        page = None

        # останавливаем воркеры и ждём завершения матчинга всего накопленного
        stop_event.set()
        if executor is not None:
            for f in worker_futures:
                f.result()
            executor.shutdown(wait=True)

        # финальный сброс очереди отклика
        if not apply_q.empty():
            log.info("финальная обработка очереди отклика (%d вакансий)", apply_q.qsize())
            apply_batch()
        log.info("итог: found=%d, applied=%d, skipped=%d, not_evaluated=%d, errors=%d",
                 stats["found"], stats["applied"], stats["skipped"], stats["not_evaluated"], stats["errors"])

    except _DailyLimitReached:
        log.warning("остановлено по дневному лимиту откликов")
    except (TargetClosedError, PlaywrightError) as e:
        log.error("браузер закрыт или ошибка навигации: %s", e)
    except KeyboardInterrupt:
        log.warning("прервано пользователем — принудительная остановка")
        stop_event.set()
        os._exit(130)
    except Exception as e:
        log.error("непредвиденная ошибка %s: %s", type(e).__name__, e)
    finally:
        stop_event.set()
        if executor is not None:
            try:
                executor.shutdown(wait=True)
            except Exception:
                pass
        if llm:
            try:
                llm.close()
            except Exception:
                pass
        if matcher:
            try:
                matcher.close()
            except Exception:
                pass
        if qgen is not None and qgen is not matcher:
            try:
                qgen.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    return stats
