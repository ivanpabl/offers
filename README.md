# offrers-hh

Автоматический поиск и отклик на вакансии hh.ru через Playwright.

## Возможности

- Поиск вакансий по конфигу и автогенерация поисковых запросов из резюме (LLM)
- Матчинг вакансий под резюме кандидата (локальная Ollama или DeepSeek)
- Автоматический отклик: выбор резюме, сопроводительное письмо, отправка
- Обработка капчи, модалок «город не совпадает», вопросов в анкете
- Дневной лимит откликов, паузы, дедупликация (SQLite)

## Требования

- Python 3.10+
- Playwright (`pip install playwright && playwright install chromium`)
- Опционально: [Ollama](https://ollama.com) с моделью `qwen2.5:7b` для матчинга

## Установка

```bash
pip install -e .
```

## Настройка

Скопируйте конфиг и заполните под себя:

```bash
cp config.example.yaml config.yaml
```

Ключевые секции `config.yaml`:

- `search` — поисковые запросы, регион, удалёнка, лимиты страниц/раундов
- `apply` — текст резюме, шаблон сопроводительного письма, дневной лимит
- `deepseek` — API DeepSeek для генерации писем (опционально)
- `match` — матчинг через Ollama (порог, модель, число воркеров)

## Использование

```bash
# Вход в аккаунт hh.ru (сессия сохранится в browser_data/)
offrers login

# Запуск поиска и отклика (--dry-run — только показать)
offrers run
offrers run --dry-run
offrers run --no-remote

# Статистика откликов
offrers status
```

## Структура

```
src/
  main.py     CLI (run / login / status)
  browser.py  сбор вакансий, матчинг, отклик
  llm.py      DeepSeek + Ollama интеграции
  config.py   pydantic-модели конфига
  storage.py  SQLite (applied, evaluated)
config.yaml        локальный конфиг (не коммитится)
config.example.yaml шаблон конфига
```

## Disclaimer

Автоматический отклик на hh.ru может нарушать правила сайта. Используйте на свой риск.
