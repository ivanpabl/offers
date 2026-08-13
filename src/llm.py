import json
import logging
import re
import httpx


log = logging.getLogger("offrers.llm")


SYSTEM_PROMPT = (
    "Ты — карьерный консультант. Напиши сопроводительное письмо кандидата "
    "на вакансию. Пиши на {language} языке. Письмо должно быть:"
    "\n- Кратким (3-5 предложений)"
    "\n- Профессиональным, без шаблонных фраз"
    "\n- С акцентом на релевантный опыт из резюме"
    "\n- С персонализацией под конкретную вакансию"
    "\nНе используй приветствие и подпись, только основной текст."
)

USER_PROMPT = (
    "Вакансия:\n{vacancy}\n\n"
    "Резюме кандидата:\n{resume}\n\n"
    "Напиши сопроводительное письмо:"
)


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", language: str = "ru",
                 max_tokens: int = 1000, temperature: float = 0.7):
        self.model = model
        self.language = language
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def generate_cover_letter(self, vacancy_text: str, resume_text: str) -> str:
        system = SYSTEM_PROMPT.format(language=self.language)
        user = USER_PROMPT.format(vacancy=vacancy_text, resume=resume_text)

        log.debug("DeepSeek запрос: model=%s, vacancy_len=%d, resume_len=%d",
                  self.model, len(vacancy_text), len(resume_text))
        resp = self.client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        log.debug("DeepSeek ответ получен: len=%d", len(content))
        return content

    def close(self):
        self.client.close()


def format_vacancy(vacancy: dict) -> str:
    parts = [vacancy.get("name", "")]

    employer = vacancy.get("employer", {})
    if employer.get("name"):
        parts.append(f"Компания: {employer['name']}")

    desc = vacancy.get("description", "")
    if desc:
        desc_clean = re.sub(r"<[^>]+>", " ", desc)
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
        if len(desc_clean) > 2000:
            desc_clean = desc_clean[:2000] + "..."
        parts.append(f"Описание: {desc_clean}")

    key_skills = vacancy.get("key_skills", [])
    if key_skills:
        skills = ", ".join(s["name"] for s in key_skills)
        parts.append(f"Навыки: {skills}")

    experience = vacancy.get("experience", {})
    if experience.get("name"):
        parts.append(f"Опыт: {experience['name']}")

    salary = vacancy.get("salary")
    if salary:
        sal_parts = []
        if salary.get("from"):
            sal_parts.append(f"от {salary['from']}")
        if salary.get("to"):
            sal_parts.append(f"до {salary['to']}")
        if salary.get("currency"):
            sal_parts.append(salary["currency"])
        parts.append(f"Зарплата: {' '.join(sal_parts)}")

    return "\n".join(parts)


MATCH_SYSTEM_PROMPT = (
    "Ты — HR-аналитик, который оценивает соответствие кандидата вакансии. "
    "Оценивай ТОЛЬКО по стеку технологий, задачам и навыкам. "
    "Полностью ИГНОРИРУЙ опыт работы кандидата и его продолжительность. "
    "Если в вакансии требуются технологии и задачи, которыми кандидат владеет, — это совпадение.\n"
    'Ответь строго в формате JSON без дополнительного текста:\n'
    '{"matches": true|false, "score": 0-100, "reason": "Краткая причина на русском (2-3 предложения)"}\n'
    "Шкала score: 0-49 не подходит, 50-69 частично подходит, 70-100 подходит."
)

MATCH_USER_PROMPT = (
    "Резюме кандидата:\n{resume}\n\n"
    "Вакансия:\n{vacancy}\n\n"
    "Подходит ли кандидат под вакансию по стеку и задачам? Верни JSON."
)


def _parse_json_response(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


COVER_LETTER_SYSTEM_PROMPT = (
    "Ты — карьерный консультант. Напиши сопроводительное письмо кандидата "
    "на конкретную вакансию. Выбери из резюме кандидата только те навыки и опыт, "
    "которые соответствуют требованиям вакансии, и опирайся на них. Письмо должно быть:"
    "\n- Кратким (3-5 предложений)"
    "\n- Профессиональным, без шаблонных фраз"
    "\n- С конкретными технологиями и задачами из резюме"
    "\n- Персонализированным под вакансию"
    "\nНе используй приветствие и подпись, только основной текст."
    '\nОтветь строго в формате JSON без дополнительного текста: {"letter": "текст письма"}'
)


QUERY_SYSTEM_PROMPT = (
    "Ты — карьерный консультант. На основе резюме кандидата составь точные "
    "поисковые запросы для сайта вакансий hh.ru. Каждый запрос должен учитывать "
    "желаемую должность, ключевой стек технологий и сферу из резюме. "
    "Покрывай разные аспекты: точная роль, основной стек, смежные роли. "
    "Используй синтаксис hh.ru: кавычки для точной фразы, минус для исключений "
    "(например -стажер -intern). Запросы должны быть короткими (2-5 слов).\n"
    'Ответь строго в формате JSON без дополнительного текста:\n'
    '{"queries": ["запрос1", "запрос2"]}'
)


class OllamaMatcher:
    def __init__(self, model_url: str = "http://localhost:11434",
                 model: str = "qwen2.5:7b", threshold: int = 60,
                 max_chars: int = 2500, timeout: int = 60):
        self.model = model
        self.threshold = threshold
        self.max_chars = max_chars
        self.client = httpx.Client(base_url=model_url, timeout=timeout)

    def generate_queries(self, resume_text: str, max_queries: int = 5) -> list[str]:
        resume = resume_text[: self.max_chars * 3]
        log.debug("Ollama: генерация запросов (max_queries=%d, resume_len=%d)", max_queries, len(resume))
        resp = self.client.post(
            "/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": QUERY_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"Резюме кандидата:\n{resume}\n\n"
                        f"Составь до {max_queries} поисковых запросов для hh.ru. Верни JSON."
                    )},
                ],
                "options": {"temperature": 0.3, "num_predict": 400},
            },
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        log.debug("Ollama ответ на запросы: %s", content[:300])

        data = _parse_json_response(content)
        queries = data.get("queries")
        if not isinstance(queries, list):
            log.warning("Ollama вернул не список запросов: %r", content[:200])
            return []
        out = []
        for q in queries:
            q = str(q).strip()
            if q and q not in out:
                out.append(q)
        return out[: max_queries]

    def is_match(self, vacancy_text: str, resume_text: str) -> tuple[bool, int, str]:
        vacancy = vacancy_text[: self.max_chars]
        resume = resume_text[: self.max_chars * 2]
        log.debug("Ollama: матчинг (vacancy_len=%d, resume_len=%d)", len(vacancy), len(resume))

        resp = self.client.post(
            "/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": MATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": MATCH_USER_PROMPT.format(
                        resume=resume, vacancy=vacancy
                    )},
                ],
                "options": {"temperature": 0.1, "num_predict": 200},
            },
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        log.debug("Ollama ответ на матчинг: %s", content[:300])

        data = _parse_json_response(content)
        score = data.get("score")
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = 0
        reason = str(data.get("reason", "")).strip()
        matches = bool(data.get("matches", False))

        decision = score >= self.threshold
        log.debug("матчинг решение: score=%s, threshold=%s, matches=%s -> %s",
                  score, self.threshold, matches, decision)
        return decision, score, reason

    def generate_cover_letter(self, vacancy_text: str, resume_text: str, language: str = "ru") -> str:
        vacancy = vacancy_text[: self.max_chars]
        resume = resume_text[: self.max_chars * 2]
        log.debug("Ollama: генерация письма (vacancy_len=%d, resume_len=%d)", len(vacancy), len(resume))
        resp = self.client.post(
            "/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": COVER_LETTER_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"Вакансия:\n{vacancy}\n\n"
                        f"Резюме кандидата:\n{resume}\n\n"
                        "Напиши сопроводительное письмо под эту вакансию. Верни JSON."
                    )},
                ],
                "options": {"temperature": 0.4, "num_predict": 220},
            },
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        log.debug("Ollama ответ на письмо: %s", content[:200])
        data = _parse_json_response(content)
        letter = str(data.get("letter", "")).strip()
        if not letter:
            letter = content.strip()
            if letter.startswith("{"):
                return ""
        letter = letter.strip('"').strip()
        return letter

    def close(self):
        self.client.close()
