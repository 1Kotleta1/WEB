import os
from typing import Final

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from news_parser import NewsParser


TELEGRAM_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN", "")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь мне тему, и я поищу свежие новости на разрешённых сайтах."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Просто пришли текстовый запрос, например:\n"
        "«криптовалюта», «выборы в США», «технологии ИИ»."
    )


def _format_news_message(query: str, items) -> str:
    if not items:
        return f"По запросу «{query}» ничего не найдено на разрешённых сайтах."

    lines: list[str] = [f"Новости по запросу «{query}»:"]  # noqa: RUF100
    for idx, item in enumerate(items, start=1):
        lines.append(
            f"\n<b>{idx}. {item.title}</b>\n"
            f"Источник: {item.source}\n"
            f"{item.url}"
        )
        if item.snippet:
            lines.append(item.snippet)
    return "\n".join(lines)


async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    query = update.message.text.strip()
    if not query:
        return

    waiting_message = await update.message.reply_text(
        "Ищу новости, подождите несколько секунд..."
    )

    # Используем Selenium-парсер в отдельном потоке, чтобы не блокировать event loop
    loop = context.application.loop

    def _run_parser() -> list:
        with NewsParser() as parser:
            return parser.search(query=query, max_results_per_site=3)

    items = await loop.run_in_executor(None, _run_parser)

    text = _format_news_message(query, items)
    await waiting_message.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Телеграм токен не найден. Установите переменную окружения TELEGRAM_BOT_TOKEN."
        )

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))

    application.run_polling()


if __name__ == "__main__":
    main()

