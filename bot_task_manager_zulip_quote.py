#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from bdateutil import relativedelta
import chardet
import re
import json
import datetime
import logging
from typing import List, Optional, Tuple
import os
import requests

import zulip
from redminelib import Redmine

# ===================== НАСТРОЙКИ ZULIP =====================

ZULIP_SITE = "https://zulip.krista.ru"
ZULIP_BOT_EMAIL = "Pomogator-bot@zulip.krista.ru"
ZULIP_API_KEY = "eImPreTOkKA3bEnMpdv1ox2yYNrT9jra"

# Стрим, где бот должен работать
ZULIP_STREAM_FILTER = "Отдел администрирования. Обработка задач"

# Топик, где бот должен читать и куда должен писать
ZULIP_BOT_TOPIC = "Помогатор"

# Полное имя бота в Zulip (как в упоминании @**...**)
BOT_FULL_NAME = "Pomogator"

client = zulip.Client(
    email=ZULIP_BOT_EMAIL,
    api_key=ZULIP_API_KEY,
    site=ZULIP_SITE,
)

# ===================== НАСТРОЙКИ REDMINE =====================

REDMINE_URL = "http://fmredmine.krista.ru"
REDMINE_USERNAME = "fm-assembly"
REDMINE_PASSWORD = "2BhBWwexCmvEC"

redmine = Redmine(
    REDMINE_URL,
    username=REDMINE_USERNAME,
    password=REDMINE_PASSWORD,
)

# Ловим оба домена:
# - copy-fmredmine.krista.ru
# - fmredmine.krista.ru
ISSUE_URL_RE = re.compile(
    r"https?://(?:copy-)?fmredmine\.krista\.ru/issues/(\d+)(?:[^\s#]*)?"
)

MENTION_RE = re.compile(r'@\*\*([^*]+)\*\*')
BOT_MENTION_RE = re.compile(rf'@\*\*{re.escape(BOT_FULL_NAME)}(?:\|\d+)?\*\*')

# где сохраняем скачанные файлы из Zulip
UPLOAD_DIR = "/opt/bots/bot3/file"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ссылки на вложения Zulip
UPLOAD_URL_RE = re.compile(
    r'(?:' + re.escape(ZULIP_SITE) + r')?(?P<path>/user_uploads/[^\s)]+)'
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


class BytesEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return obj.decode("utf-8")
        return json.JSONEncoder.default(self, obj)


# ===================== ХЕЛПЕРЫ =====================

def get_message_topic(msg) -> str:
    """Безопасно получаем топик stream-сообщения."""
    return (msg.get("subject") or msg.get("topic") or "").strip()


def zulip_reply(msg, text: str) -> None:
    """В стриме бот всегда пишет в топик ZULIP_BOT_TOPIC."""
    if msg["type"] == "stream":
        client.send_message(
            {
                "type": "stream",
                "to": msg["display_recipient"],
                "topic": ZULIP_BOT_TOPIC,
                "content": text,
            }
        )
    else:
        client.send_message(
            {
                "type": "private",
                "to": [msg["sender_email"]],
                "content": text,
            }
        )


def strip_emoji(text: str) -> str:
    """Убираем emoji и другие 4-байтные символы, чтобы MySQL не падал."""
    return re.sub(r'[\U00010000-\U0010FFFF]', '', text)
def remove_code_blocks(text: str) -> str:
    """
    Удаляем fenced code blocks ``` ... ```
    чтобы строки из них не попадали в subject.
    """
    if not text:
        return text
    return re.sub(r'```.*?```', '', text, flags=re.S)

def detect_cp1251(text: str) -> str:
    enc = chardet.detect(text.encode()).get("encoding")
    if enc != "windows-1251":
        text = text.encode("windows-1251", errors="ignore").decode("windows-1251")
    return text


def split_quote_and_body(content: str) -> Tuple[Optional[str], str]:
    m = re.match(r'(?s)^(?P<prefix>.*?)```quote\s*(?P<quoted>.*?)\s*```(?P<suffix>.*)$', content)
    if m:
        quoted = m.group("quoted").strip("\n")
        body = (m.group("prefix") + "\n" + m.group("suffix")).strip("\n")
        return quoted, body
    return None, content.strip("\n")


def extract_subject_candidate(text: Optional[str]) -> Optional[str]:
    """
    Возвращает первую осмысленную строку для темы задачи.

    Правила:
    - удаляем упоминания (@**...**)
    - пропускаем пустые строки
    - пропускаем строки-команды вида '+ #123' / '+ url'
    - пропускаем строки, состоящие только из URL
    """
    if not text:
        return None

    cmd_pattern = r'^\s*\+\s*(?:#\d+|https?://[^\s]*/issues/\d+)\s*$'
    cleaned = MENTION_RE.sub("", text)

    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(cmd_pattern, line):
            continue
        if re.fullmatch(r'https?://\S+', line):
            continue
        return line

    return None


def find_issue_ids(text: str) -> List[str]:
    return ISSUE_URL_RE.findall(text or "")


def get_issue_id_from_plus(text: str) -> Optional[int]:
    """
    Режим "+ задача": ищем "+ #id" или "+ <url>"
    """
    m = re.search(
        r'\+\s*(?:#(?P<id1>\d+)|https?://[^\s]*/issues/(?P<id2>\d+))',
        text or ""
    )
    if not m:
        return None
    issue_id_str = m.group("id1") or m.group("id2")
    try:
        return int(issue_id_str)
    except (TypeError, ValueError):
        return None


def normalize_mention_name(name: str) -> str:
    """
    Zulip может прислать mention как:
      @**Игорь Милютин**
    или
      @**Игорь Милютин|123**
    Берем только имя.
    """
    return (name or "").split("|", 1)[0].strip()


def normalize_person_name(name: str) -> str:
    """
    Нормализация имени для сравнения:
    - lower
    - ё -> е
    - схлопываем пробелы
    """
    name = (name or "").strip().lower().replace("ё", "е")
    name = re.sub(r"\s+", " ", name)
    return name


def get_first_issue_id_from_text(text: str) -> Optional[int]:
    ids = find_issue_ids(text or "")
    if not ids:
        return None
    try:
        return int(ids[0])
    except (TypeError, ValueError):
        return None


def has_bot_mention(text: str) -> bool:
    """Проверяем, что в тексте есть упоминание бота."""
    if not text:
        return False
    return BOT_MENTION_RE.search(text) is not None


def is_reply_to_bot_message(content: str) -> bool:
    """
    Проверяем, что это reply на сообщение бота.
    """
    if not content:
        return False

    return re.search(
        rf'@_\*\*{re.escape(BOT_FULL_NAME)}(?:\|\d+)?\*\*.*?```quote',
        content,
        re.S
    ) is not None


def looks_like_bot_issue_quote(quoted: Optional[str]) -> bool:
    """
    Проверяем, что цитата похожа на сообщение бота о созданной задаче:
    - есть ссылка на issue
    - есть строка 'Ответственный:'
    """
    if not quoted:
        return False

    has_issue = get_first_issue_id_from_text(quoted) is not None
    has_assignee_line = "ответственный:" in quoted.lower()

    return has_issue and has_assignee_line


# ===================== НАЗНАЧЕНИЕ ПО УПОМИНАНИЮ =====================

ASSIGNEE_RAW_MAP = {
    "Игорь Милютин": 408,
    "Николай Погодин": 99,
    "Данила Мухин": 508,
    "Дмитрий Вороненков": 532,
    "Денис Лебедев": 446,
    "Николай Ефимов": 355,
    "Максим Анисимов": 506,
    "Константин Полуэктов": 466,
    "Александр Куриченков": 591,
    "Ринат Ламзиков": 579,
    "Константин Маркелов": 595,
    "Артём Чистяков": 632,
    "Иван Петряев": 709,
}


def build_assignee_map(raw_map):
    """
    Строим карту алиасов:
    - 'имя фамилия'
    - 'фамилия имя'
    """
    result = {}

    for display_name, user_id in raw_map.items():
        norm = normalize_person_name(display_name)
        result[norm] = (user_id, display_name)

        parts = norm.split()
        if len(parts) == 2:
            swapped = f"{parts[1]} {parts[0]}"
            result[swapped] = (user_id, display_name)

    return result


ASSIGNEE_MAP = build_assignee_map(ASSIGNEE_RAW_MAP)


def find_assignee_mention(text: str) -> Optional[Tuple[int, str]]:
    """
    Ищем упоминание сотрудника из ASSIGNEE_MAP.
    Возвращаем (assigned_to_id, canonical_assignee_name) или None.
    """
    if not text:
        return None

    mentions = list(MENTION_RE.finditer(text))
    bot_name_norm = BOT_FULL_NAME.strip().lower()

    # сначала смотрим рядом с упоминанием бота
    for m in mentions:
        name_raw = normalize_mention_name(m.group(1))
        if name_raw.strip().lower() == bot_name_norm:
            start, end = m.span()
            ctx = text[max(0, start - 120): end + 120]

            for m2 in MENTION_RE.finditer(ctx):
                assignee_name_raw = normalize_mention_name(m2.group(1))
                if assignee_name_raw.strip().lower() == bot_name_norm:
                    continue

                found = ASSIGNEE_MAP.get(normalize_person_name(assignee_name_raw))
                if found:
                    assigned_to_id, canonical_name = found
                    logging.info(
                        "Найден исполнитель рядом с ботом: raw='%s', canonical='%s', id=%s",
                        assignee_name_raw, canonical_name, assigned_to_id
                    )
                    return assigned_to_id, canonical_name

    # если рядом с ботом не нашли — берем первое валидное упоминание
    for m in mentions:
        assignee_name_raw = normalize_mention_name(m.group(1))
        if assignee_name_raw.strip().lower() == bot_name_norm:
            continue

        found = ASSIGNEE_MAP.get(normalize_person_name(assignee_name_raw))
        if found:
            assigned_to_id, canonical_name = found
            logging.info(
                "Найден исполнитель: raw='%s', canonical='%s', id=%s",
                assignee_name_raw, canonical_name, assigned_to_id
            )
            return assigned_to_id, canonical_name

    return None


def choose_assignee_from_body(body: str, default_id: int = 408) -> int:
    found = find_assignee_mention(body)
    if found:
        return found[0]
    return default_id


# ===================== РАБОТА С ВЛОЖЕНИЯМИ ИЗ ZULIP =====================

def sanitize_filename(name: str, fallback: str = "upload.bin") -> str:
    """Имя файла, совместимое с windows-1251."""
    name = os.path.basename(name)
    safe = name.encode("windows-1251", errors="ignore").decode("windows-1251").strip()
    return safe or fallback


def download_zulip_file(path: str) -> Optional[Tuple[str, str]]:
    """
    Скачивает файл с Zulip по пути /user_uploads/... ,
    сохраняет в UPLOAD_DIR, возвращает (full_path, filename).
    """
    path = path.split("?")[0]
    url = ZULIP_SITE + path
    try:
        resp = requests.get(
            url,
            auth=(ZULIP_BOT_EMAIL, ZULIP_API_KEY),
            stream=True,
            timeout=30,
        )
    except Exception as e:
        logging.error("Ошибка при скачивании %s: %s", url, e)
        return None

    if resp.status_code != 200:
        logging.error("Не удалось скачать %s: статус %s", url, resp.status_code)
        return None

    filename_raw = path.split("/")[-1]
    filename = sanitize_filename(filename_raw)
    full_path = os.path.join(UPLOAD_DIR, filename)

    try:
        with open(full_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        logging.error("Ошибка записи файла %s: %s", full_path, e)
        return None

    return full_path, filename


def collect_uploads_from_content(content: str) -> List[dict]:
    """
    Ищем в тексте ссылки на /user_uploads/... ,
    скачиваем файлы и формируем список uploads для Redmine.
    """
    uploads: List[dict] = []

    for m in UPLOAD_URL_RE.finditer(content):
        rel_path = m.group("path")
        res = download_zulip_file(rel_path)
        if not res:
            continue
        full_path, filename = res
        uploads.append({"path": full_path, "filename": filename})

    return uploads


# ===================== СОЗДАНИЕ / КОММЕНТАРИИ / ПЕРЕНАЗНАЧЕНИЕ =====================

def create_or_update_issue_from_message(msg, content: str) -> None:
    """
    Логика:
      - если есть "+ #id" или "+ url" -> добавить комментарий к существующей задаче;
      - если это reply на сообщение бота с задачей и в reply упомянут сотрудник -> переназначить задачу;
      - иначе -> создать новую задачу (упоминание бота обязательно).
    """
    logging.info("Обрабатываю сообщение от %s", msg["sender_full_name"])

    quoted, body = split_quote_and_body(content)

    logging.info("quoted=%r", quoted)
    logging.info("body=%r", body)

    # Собираем вложения
    uploads = collect_uploads_from_content(content)

    # ping Redmine
    try:
        requests.get(REDMINE_URL, verify=False, timeout=5)
    except Exception:
        pass

    date_main = datetime.date.today() + relativedelta(days=7)

    # ===== Определяем режим =====
    append_issue_id = get_issue_id_from_plus(body) if body else None
    quoted_issue_id = get_first_issue_id_from_text(quoted) if quoted else None
    reply_assignee = find_assignee_mention(body)

    can_reassign_from_reply = (
            append_issue_id is None
            and quoted_issue_id is not None
            and reply_assignee is not None
            and (
                    is_reply_to_bot_message(content)
                    or looks_like_bot_issue_quote(quoted)
            )
    )

    logging.info(
        "append_issue_id=%s quoted_issue_id=%s reply_assignee=%s can_reassign_from_reply=%s",
        append_issue_id, quoted_issue_id, reply_assignee, can_reassign_from_reply
    )

    # ===== Режим переназначения =====
    if can_reassign_from_reply:
        assigned_to_id, assignee_name = reply_assignee
        issue_url = f"https://fmredmine.krista.ru/issues/{quoted_issue_id}"

        try:
            redmine.issue.update(
                resource_id=quoted_issue_id,
                assigned_to_id=assigned_to_id,
            )

            issue_obj = redmine.issue.get(quoted_issue_id)

            zulip_reply(
                msg,
                f"Задача переназначена на {assignee_name}\n"
                f"❗ {issue_url}\n"
                f"Ответственный: {issue_obj.assigned_to}",
            )
        except Exception as e:
            logging.exception("Ошибка при переназначении задачи %s", quoted_issue_id)
            zulip_reply(
                msg,
                f"Не удалось переназначить задачу #{quoted_issue_id}\n"
                f"❗ {issue_url}\n{e}",
            )
        return

    # ===== Если ЭТО НОВАЯ ЗАДАЧА, то обязательно должно быть упоминание бота =====
    if append_issue_id is None:
        if not has_bot_mention(body or ""):
            logging.info("Новая задача без упоминания бота — игнорируем сообщение")
            return

    # Убираем code blocks перед определением темы
    body_no_code = remove_code_blocks(body or "")
    quoted_no_code = remove_code_blocks(quoted or "")
    
    subject_raw = (
            extract_subject_candidate(body_no_code)
            or extract_subject_candidate(quoted_no_code)
            or "Тема задачи"
    )

    subject_raw = strip_emoji(subject_raw)
    subject = detect_cp1251(subject_raw)[:255]

    # ===== Формируем описание =====
    description_parts = []

    initiator_line = f"Инициатор: {msg['sender_full_name']} ({msg['sender_email']})"
    description_parts.append(initiator_line)

    # комментарий пользователя (body)
    user_comment_text = None
    if body:
        if append_issue_id is None:
            user_comment_text = body.strip()
        else:
            cmd_pattern = r'^\s*\+\s*(?:#\d+|https?://[^\s]*/issues/\d+)\s*$'
            blines = body.splitlines()
            cleaned_lines = [line for line in blines if not re.search(cmd_pattern, line)]
            user_comment_text = "\n".join(cleaned_lines).strip()

    if user_comment_text:
        user_comment_text = strip_emoji(user_comment_text)
        user_comment_text = detect_cp1251(user_comment_text)
        description_parts.append(f"<pre>\n{user_comment_text}\n</pre>")

    # цитата как дополнительная информация
    if quoted:
        quoted_clean = strip_emoji(quoted)
        quoted_clean = detect_cp1251(quoted)
        description_parts.append("Дополнительная информация:\n<pre>\n" + quoted_clean + "\n</pre>")

    description = "\n\n".join(description_parts)
    description = strip_emoji(description)
    description = detect_cp1251(description)

    # ===== Режим "+ задача": добавить комментарий =====
    if append_issue_id is not None:
        base_issue_url = f"https://fmredmine.krista.ru/issues/{append_issue_id}"
        try:
            update_kwargs = {
                "resource_id": append_issue_id,
                "notes": description,
            }
            if uploads:
                update_kwargs["uploads"] = uploads

            redmine.issue.update(**update_kwargs)

            issue_obj = redmine.issue.get(append_issue_id, include=["journals"])

            journals_with_notes = [
                j for j in getattr(issue_obj, "journals", [])
                if getattr(j, "notes", None)
            ]
            if journals_with_notes:
                note_idx = len(journals_with_notes)
                comment_url = f"{base_issue_url}#note-{note_idx}"
            else:
                comment_url = base_issue_url

            assigned_str = (
                f"Ответственный: {issue_obj.assigned_to}"
                if hasattr(issue_obj, "assigned_to") else ""
            )
            msg_text = f"Комментарий добавлен в задачу\n➕ {comment_url}"
            if assigned_str:
                msg_text += f"\n{assigned_str}"

            zulip_reply(msg, msg_text)
        except Exception as e:
            logging.exception("Ошибка при добавлении комментария в задачу %s", append_issue_id)
            zulip_reply(
                msg,
                f"Не удалось добавить комментарий в задачу #{append_issue_id}\n"
                f"❗ {base_issue_url}\n{e}",
            )
        return

    # ===== НОВАЯ ЗАДАЧА =====
    assigned_to_id = choose_assignee_from_body(body, default_id=408)
    found_assignee = find_assignee_mention(body)

    if found_assignee:
        logging.info(
            "Создание задачи. Назначаю на %s (id=%s)",
            found_assignee[1], found_assignee[0]
        )
    else:
        logging.info(
            "Создание задачи. Исполнитель не найден, назначаю по умолчанию id=%s",
            assigned_to_id
        )

    try:
        create_kwargs = {
            "project_id": 167,
            "subject": subject,
            "tracker_id": 3,
            "description": description,
            "status_id": 7,
            "priority_id": 6,
            "assigned_to_id": assigned_to_id,
            "due_date": date_main,
        }
        if uploads:
            create_kwargs["uploads"] = uploads

        issue = redmine.issue.create(**create_kwargs)
    except Exception as e:
        logging.exception("Ошибка при создании новой задачи")
        zulip_reply(msg, f"Ошибка при создании новой задачи в Redmine: {e}")
        return

    issue_url = f"https://fmredmine.krista.ru/issues/{issue.id}"
    zulip_reply(
        msg,
        f"{subject}\n❗ {issue_url}\nОтветственный: {issue.assigned_to}",
    )


# ===================== ОБРАБОТЧИК СОБЫТИЙ ZULIP =====================

def handle_event(event):
    if event["type"] != "message":
        return

    msg = event["message"]

    # не реагируем на свои сообщения
    if msg["sender_email"] == ZULIP_BOT_EMAIL:
        return

    # работаем только со stream-сообщениями
    if msg["type"] != "stream":
        return

    # фильтр по стриму
    if ZULIP_STREAM_FILTER and msg["display_recipient"] != ZULIP_STREAM_FILTER:
        return

    # фильтр по топику
    if ZULIP_BOT_TOPIC and get_message_topic(msg) != ZULIP_BOT_TOPIC:
        return

    content = (msg.get("content") or "").strip()
    if not content:
        return

    create_or_update_issue_from_message(msg, content)


def main():
    logging.info(
        "Zulip copy-redmine bot (создание/комментарии/переназначение/вложения) запущен"
    )

    narrow = None
    if ZULIP_STREAM_FILTER and ZULIP_BOT_TOPIC:
        narrow = [
            ["stream", ZULIP_STREAM_FILTER],
            ["topic", ZULIP_BOT_TOPIC],
        ]
    elif ZULIP_STREAM_FILTER:
        narrow = [["stream", ZULIP_STREAM_FILTER]]

    client.call_on_each_event(
        handle_event,
        event_types=["message"],
        narrow=narrow,
    )


if __name__ == "__main__":
    main()
