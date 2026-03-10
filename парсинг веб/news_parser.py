import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import undetected_chromedriver as uc
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    snippet: Optional[str] = None


class NewsParser:
    def __init__(
        self,
        allowed_sites_path: Path | str = "allowed_sites.json",
        headless: bool = True,
        page_load_timeout: int = 20,
        adblock_extension_path: Path | str | None = None,
    ) -> None:
        self.allowed_sites_path = Path(allowed_sites_path)
        self.headless = headless
        self.page_load_timeout = page_load_timeout
        self._driver = None
        self._sites = self._load_allowed_sites()
        self.adblock_extension_path = (
            Path(adblock_extension_path) if adblock_extension_path else None
        )

    def _extract_keywords(self, query: str) -> list[str]:
        """
        Преобразуем фразу пользователя в набор ключевых слов.
        Убираем короткие и служебные слова, используем для более
        "умного" поиска по контексту.
        """
        raw = re.split(r"\W+", query.lower())
        words = [w for w in raw if len(w) >= 4]

        # Простые синонимы для некоторых частых запросов
        synonyms: dict[str, list[str]] = {
            "обстановка": ["ситуация", "положение", "обострение", "напряженность"],
            "мир": ["мировой", "международный", "зарубежный"],
            "война": ["боевые", "военный", "фронт", "операция"],
            "доллар": ["usd", "курс доллара", "валюта"],
        }

        expanded: list[str] = []
        for w in words:
            expanded.append(w)
            if w in synonyms:
                expanded.extend(synonyms[w])

        uniq: list[str] = []
        seen: set[str] = set()
        for w in expanded:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        return uniq

    def _close_overlays(self, driver, timeout: int = 5) -> None:
        """
        Пытаемся закрыть типичные модальные окна / баннеры:
        крестики, кнопки 'Закрыть', иконки с aria-label и т.п.
        Функция максимально безопасная: все ошибки глушатся.
        """
        base_candidates = [
            # Кнопки/иконки с текстом
            (By.XPATH, "//button[contains(translate(., 'ЗАКРЫТЬ', 'закрыть'), 'закрыть')]"),
            (By.XPATH, "//div[contains(translate(., 'ЗАКРЫТЬ', 'закрыть'), 'закрыть')]"),
            # Крестики / общие классы
            (By.CSS_SELECTOR, "button[aria-label*='Закрыть' i]"),
            (By.CSS_SELECTOR, "button[title*='Закрыть' i]"),
            (By.CSS_SELECTOR, "[class*='close' i]"),
            (By.CSS_SELECTOR, "[class*='popup-close' i]"),
            (By.CSS_SELECTOR, "[class*='modal__close' i]"),
            (By.CSS_SELECTOR, "[class*='banner-close' i]"),
        ]

        # Специальные селекторы для РБК (модальные окна/подписки)
        rbc_candidates = [
            (By.CSS_SELECTOR, ".rbc-popup__close"),
            (By.CSS_SELECTOR, ".js-rbc-popup-close"),
            (By.CSS_SELECTOR, ".ui-kit-popup__close"),
            (By.CSS_SELECTOR, ".modal__close, .modal__close-btn"),
        ]

        candidates = base_candidates
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        if "rbc.ru" in current_url:
            candidates = rbc_candidates + base_candidates

        deadline = time.time() + timeout
        while time.time() < deadline:
            clicked = False
            for by, value in candidates:
                try:
                    el = WebDriverWait(driver, 1).until(
                        EC.element_to_be_clickable((by, value))
                    )
                    el.click()
                    time.sleep(0.5)
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                # Дадим странице обновиться после закрытия окна
                time.sleep(0.5)
                break

    def _load_allowed_sites(self) -> list[dict]:
        if not self.allowed_sites_path.exists():
            raise FileNotFoundError(
                f"Файл с разрешёнными сайтами не найден: {self.allowed_sites_path}"
            )
        with self.allowed_sites_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Файл allowed_sites.json должен содержать список сайтов.")
        return data

    def _scroll_page(self, driver, steps: int = 6, pause: float = 0.7) -> None:
        """
        Догружаем ленту новостей прокруткой (часто нужно для РБК).
        """
        try:
            last_height = driver.execute_script("return document.body.scrollHeight")
        except Exception:
            last_height = None

        for _ in range(max(0, steps)):
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                break
            time.sleep(pause)
            self._close_overlays(driver, timeout=2)
            try:
                new_height = driver.execute_script("return document.body.scrollHeight")
            except Exception:
                new_height = None
            if last_height is not None and new_height is not None and new_height == last_height:
                break
            last_height = new_height

    def _init_driver(self):
        if self._driver is not None:
            return self._driver

        def _build_options() -> uc.ChromeOptions:
            opts = uc.ChromeOptions()
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )

            # Поддержка adblock‑расширения, если указали путь:
            # - распакованная папка с manifest.json
            # - или .crx файл
            if self.adblock_extension_path and self.adblock_extension_path.exists():
                p = self.adblock_extension_path
                if p.is_file() and p.suffix.lower() == ".crx":
                    try:
                        opts.add_extension(str(p))
                    except Exception:
                        pass
                elif p.is_dir():
                    manifest = p / "manifest.json"
                    if manifest.exists():
                        opts.add_argument(f"--load-extension={p}")
            return opts

        def _start(version_main: int | None = None):
            return uc.Chrome(
                options=_build_options(),
                headless=self.headless,
                version_main=version_main,
                use_subprocess=True,
            )

        try:
            driver = _start()
        except SessionNotCreatedException as e:
            # Частая ситуация: скачался драйвер не под ту major-версию Chrome.
            # Пробуем вытащить major-версию из текста ошибки и запустить повторно.
            msg = str(e)
            m = re.search(r"Current browser version is (\d+)\.", msg)
            if not m:
                raise
            major = int(m.group(1))
            driver = _start(version_main=major)
        driver.set_page_load_timeout(self.page_load_timeout)
        self._driver = driver
        return driver

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except OSError:
                # undetected-chromedriver иногда бросает WinError 6 при деструкторе/закрытии
                pass
            self._driver = None

    def __enter__(self) -> "NewsParser":
        self._init_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _build_search_url(self, site_cfg: dict, query: str) -> str:
        template = site_cfg.get("search_url")
        base = site_cfg.get("base_url", "").rstrip("/")
        if template:
            # Если шаблон есть — подставляем {query}
            return template.replace("{query}", query)
        # Если search_url не задан, просто открываем базовый список новостей
        return base

    def search(
        self,
        query: str,
        max_results_per_site: int = 12,
        filter_by_query: bool = True,
    ) -> List[NewsItem]:
        driver = self._init_driver()
        results: List[NewsItem] = []

        for site in self._sites:
            search_url = self._build_search_url(site, query)
            try:
                driver.get(search_url)
            except Exception:
                continue

            # Небольшая пауза, чтобы страница успела прогрузиться и не вызывать лишних подозрений
            time.sleep(2)
            # Пытаемся закрыть возможные оверлеи/баннеры (подписка, реклама и т.п.)
            self._close_overlays(driver)
            # Догружаем ленту прокруткой, чтобы было больше новостей для поиска
            self._scroll_page(driver, steps=8, pause=0.8)

            site_results: List[NewsItem] = []

            result_selector = site.get("result_item_selector", "article")
            title_selector = site.get("title_selector", "h2, h3, a")
            link_selector = site.get("link_selector", "a")
            snippet_selector = site.get("snippet_selector", "p")

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, result_selector))
                )
            except Exception:
                continue

            items = driver.find_elements(By.CSS_SELECTOR, result_selector)
            count = 0
            for item in items:
                if count >= max_results_per_site:
                    break
                try:
                    title_el = item.find_element(By.CSS_SELECTOR, title_selector)
                except Exception:
                    continue

                title = title_el.text.strip()
                if not title:
                    continue

                try:
                    # Если сам item уже ссылка - используем её, иначе ищем вложенную ссылку
                    if item.tag_name.lower() == "a":
                        link_el = item
                    else:
                        link_el = item.find_element(By.CSS_SELECTOR, link_selector)
                    url = link_el.get_attribute("href") or ""
                except Exception:
                    url = ""

                snippet = ""
                try:
                    snippet_el = item.find_element(By.CSS_SELECTOR, snippet_selector)
                    snippet = snippet_el.text.strip()
                except Exception:
                    snippet = ""

                if not url:
                    continue

                site_results.append(
                    NewsItem(
                        source=site.get("name", site.get("base_url", "")),
                        title=title,
                        url=url,
                        snippet=snippet or None,
                    )
                )
                count += 1

            # На этом этапе мы собрали список новостей с ленты.
            # Теперь, если нужно, фильтруем по запросу и при необходимости
            # заходим внутрь каждой новости и ищем совпадения уже в тексте.
            keywords = self._extract_keywords(query) if filter_by_query else []
            article_selector = site.get("article_selector")

            for item in site_results:
                if not filter_by_query or not keywords:
                    results.append(item)
                    continue

                text_to_check = (item.title + " " + (item.snippet or "")).lower()
                if any(k in text_to_check for k in keywords):
                    results.append(item)
                    continue

                # Глубокий просмотр: переходим на страницу новости и ищем запрос в тексте статьи
                if article_selector and item.url:
                    try:
                        driver.get(item.url)
                        time.sleep(1)
                        self._close_overlays(driver)
                        # Иногда текст грузится ниже первого экрана
                        self._scroll_page(driver, steps=3, pause=0.6)
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, article_selector))
                        )
                        article_el = driver.find_element(By.CSS_SELECTOR, article_selector)
                        article_text = article_el.text.strip()
                    except Exception:
                        article_text = ""

                    if article_text:
                        full_text = (item.title + " " + article_text).lower()
                        if any(k in full_text for k in keywords):
                            # Обновляем сниппет более информативным фрагментом статьи
                            snippet = article_text[:400]
                            if len(article_text) > 400:
                                snippet += "…"
                            item.snippet = snippet
                            results.append(item)

        return results


if __name__ == "__main__":
    # Интерактивный режим для запуска из консоли.
    # 1) Спрашиваем тему.
    # 2) Если ничего не найдено, уточняем жанр/характер вопроса и пробуем ещё раз.

    query = input("Введите тему для поиска новостей: ")

    GENRES: dict[str, dict[str, object]] = {
        "1": {
            "title": "Политика / геополитика / конфликты",
            "extra": ["политика", "геополитика", "конфликт", "война", "санкции"],
        },
        "2": {
            "title": "Экономика / финансы / валюта",
            "extra": ["экономика", "финансы", "курс", "валюта", "рынок"],
        },
        "3": {
            "title": "Общество / социальные темы",
            "extra": ["общество", "социальный", "население", "жители"],
        },
        "4": {
            "title": "Технологии / IT / наука",
            "extra": ["технологии", "ИТ", "цифровой", "искусственный интеллект", "наука"],
        },
        "5": {
            "title": "Спорт",
            "extra": ["спорт", "чемпионат", "матч", "турнир"],
        },
    }

    # Для отладки запускаем браузер в видимом режиме (headless=False),
    # чтобы можно было наблюдать, что делает Selenium.
    adblock_path = (
        r"C:\Users\dobra\Desktop\work\парсинг веб\adblock-plus-crx-master"
        r"\adblock-plus-crx-master\bin\Adblock-Plus_v1.12.4.crx"
    )
    with NewsParser(headless=False, adblock_extension_path=adblock_path) as parser:
        news = parser.search(query, filter_by_query=True)

        if not news:
            print(f"По запросу «{query}» точных новостей не найдено.")
            print("Уточните, к какому жанру больше относится ваш запрос:")
            for key, cfg in GENRES.items():
                print(f"{key}. {cfg['title']}")
            choice = input("Введите номер жанра (или просто Enter, чтобы пропустить): ").strip()

            if choice in GENRES:
                extras = GENRES[choice]["extra"]
                # Для жанрового поиска используем только ключевые слова жанра,
                # чтобы отвязаться от неудачной формулировки пользователя.
                genre_query = " ".join(extras)
                print(f"Пробуем поиск по жанру: «{GENRES[choice]['title']}»...")
                news = parser.search(genre_query, filter_by_query=True)

    if not news:
        print("Под ваш запрос новости на разрешённых сайтах не найдены.")
    else:
        for item in news:
            print(f"[{item.source}] {item.title}\n{item.url}\n{item.snippet or ''}\n")

