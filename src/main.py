import logging

import click
from rich.console import Console
from rich.table import Table
from playwright.sync_api import sync_playwright
from .config import load_config
from .logger import setup_logging
from .storage import get_connection, get_stats
from .browser import (
    USER_DATA_DIR,
    login,
    search_and_apply_browser,
)


console = Console()
log = logging.getLogger("offrers.main")


@click.group()
def cli():
    pass


@cli.command()
@click.option("--config", "-c", default="config.yaml", help="Путь к конфигу")
@click.option("--dry-run", is_flag=True, help="Только показать, не откликаться")
@click.option("--remote/--no-remote", default=None, help="Только удалённая работа")
@click.option("--debug", "-d", is_flag=True, help="Подробные debug-логи в терминал")
def run(config: str, dry_run: bool, remote: bool | None, debug: bool):
    """Поиск и отклик на вакансии через браузер"""
    setup_logging(debug)
    cfg = load_config(config)

    if remote is not None:
        cfg.search.remote = remote

    if not USER_DATA_DIR.exists():
        console.print("[yellow]Сначала выполните: offrers login[/yellow]")
        return

    console.print("[bold]Запуск браузера...[/bold]")
    dump = cfg.model_dump()
    if dump.get("deepseek", {}).get("api_key"):
        dump["deepseek"]["api_key"] = "***"
    log.debug("config loaded: %s", dump)
    log.debug("dry_run=%s, remote=%s", dry_run, remote)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        stats = search_and_apply_browser(ctx, cfg, dry_run=dry_run)
        ctx.close()

    table = Table(title="Результат")
    table.add_column("Метрика")
    table.add_column("Значение", justify="right")
    table.add_row("Найдено", str(stats["found"]))
    table.add_row("Откликов", str(stats["applied"]))
    table.add_row("Пропущено", str(stats["skipped"]))
    table.add_row("Не оценено", str(stats.get("not_evaluated", 0)))
    table.add_row("Ошибок", str(stats["errors"]))
    console.print(table)


@cli.command("login")
@click.option("--debug", "-d", is_flag=True, help="Подробные debug-логи в терминал")
def browser_login(debug: bool):
    """Вход в аккаунт hh.ru через браузер (сессия сохранится в профиле)"""
    setup_logging(debug)
    log.debug("login: user_data_dir=%s", USER_DATA_DIR)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        login(ctx)
        ctx.close()


@cli.command()
@click.option("--debug", "-d", is_flag=True, help="Подробные debug-логи в терминал")
def status(debug: bool):
    setup_logging(debug)
    conn = get_connection()
    stats = get_stats(conn)
    conn.close()

    table = Table(title="Статистика")
    table.add_column("Метрика")
    table.add_column("Значение", justify="right")
    table.add_row("Всего откликов", str(stats["total"]))
    table.add_row("Сегодня", str(stats["today"]))
    console.print(table)


if __name__ == "__main__":
    cli()
