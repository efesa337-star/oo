"""Пользовательские хендлеры: настройки, игноры, фильтры, ЛС, репорты, эхо."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    LinkPreviewOptions,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
    ReplyParameters,
)

import config
import crypto
import delivery
import keyboards
import logs
import moderation
import textutil
import timeutil
from db import db
from texts import LANGS, t

router = Router()

async def answer(message: Message, text: str, **kwargs):
    """Ответ бота всегда цитирует твоё сообщение, а не улетает в пустоту."""
    kwargs.setdefault(
        "reply_parameters",
        ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
    )
    return await message.answer(text, **kwargs)


log = logging.getLogger("echo")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# token -> (текст, entities, текстовое ли сообщение) для подтверждения правки
PENDING_EDITS: dict[str, tuple[str, list, bool]] = {}


# --------------------------------------------------------------------------- #
#                              вспомогательное                                 #
# --------------------------------------------------------------------------- #

async def sync_username(message: Message, user: dict[str, Any]) -> None:
    """Шифруем юзернейм при входе и заново — только если он реально сменился."""
    if not user["keep_username"]:
        return
    current = message.from_user.username
    digest = crypto.fingerprint(current) if current else None
    if digest == user["username_hash"]:
        return

    blob = crypto.encrypt(current or f"id{user['id']}")
    await db.update(
        user["id"],
        username_enc=blob,
        username_hash=digest,
        name_enc=crypto.encrypt(message.from_user.full_name),
    )
    key = "username_changed" if user["username_hash"] else "username_encrypted"
    user["username_enc"], user["username_hash"] = blob, digest
    await answer(message, t(user["lang"], key, blob=blob))


def echo_markup(message: Message, user: dict[str, Any], with_delete: Optional[str] = None):
    return keyboards.echo_kb(
        user,
        display_name=message.from_user.full_name,
        username=message.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
        with_delete=with_delete,
    )


async def profile_markup(user: dict[str, Any]):
    return keyboards.profile_kb(
        user,
        ignores=await db.ignore_count(user["id"]),
        pm_blocks=await db.pm_block_count(user["id"]),
    )


async def refresh_kb(call: CallbackQuery, user: dict[str, Any]) -> None:
    # ПОЛНОСТЬЮ ПЕРЕПИСАНО: Никакого переключения на клавиатуру профиля из меню /tag!
    # Если callback прилетел из меню цветов или настроек тега, обновляем строго меню тега.
    markup = keyboards.tag_kb(user, moderation.is_staff(user))
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


# ссылки на живые задачи: без этого asyncio может собрать их сборщиком мусора
_AUTODEL_TASKS: set[asyncio.Task] = set()


async def autodelete_later(bot: Bot, ref: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        removed = await moderation.delete_message(bot, ref)
        log.info("автоудаление ref=%s: снято копий %s", ref, removed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("автоудаление ref=%s упало: %s", ref, exc)


async def schedule_autodelete(bot: Bot, ref: int, delay: int) -> None:
    """Срок пишем в базу — переживёт перезапуск, а задача сработает вовремя."""
    await db.mark_message(ref, delete_at=int(time.time()) + delay)
    task = asyncio.create_task(autodelete_later(bot, ref, delay))
    _AUTODEL_TASKS.add(task)
    task.add_done_callback(_AUTODEL_TASKS.discard)


def is_stub(record: Optional[dict]) -> bool:
    return bool(record and (record["stub"] or record["deleted"]))


async def author_of_reply(message: Message) -> tuple[Optional[dict], Optional[dict]]:
    """По реплаю возвращает (запись сообщения, автора)."""
    if not message.reply_to_message:
        return None, None
    record = await db.message_by_copy(message.chat.id, message.reply_to_message.message_id)
    if record:
        return record, await db.get_user(record["author_id"])
    peer = await db.get_pm(message.chat.id, message.reply_to_message.message_id)
    if peer:
        return None, await db.get_user(peer)
    return None, None


# --------------------------------------------------------------------------- #
#                                регистрация                                   #
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, user: dict[str, Any]):
    if not user["active"]:
        await db.update(user["id"], active=1)
    if not user["registered"]:
        await answer(message, 
            t(user["lang"], "greet", name=html.escape(message.from_user.full_name)),
            reply_markup=keyboards.confirm_start_kb(),
        )
        return
    await answer(message, t(user["lang"], "start_help"))


@router.callback_query(F.data == "reg:ok")
async def cb_register(call: CallbackQuery, user: dict[str, Any]):
    if not user["registered"]:
        await db.update(user["id"], registered=1, active=1)
        await logs.event("START", f"Новый пользователь {logs.who(user)}")
    await call.message.edit_text(
        t(
            user["lang"], "registered",
            name=html.escape(call.from_user.full_name),
            owner=config.OWNER_USERNAME,
        )
    )
    await call.answer()


# --------------------------------------------------------------------------- #
#                              простые команды                                 #
# --------------------------------------------------------------------------- #

@router.message(Command("help"))
async def cmd_help(message: Message, user: dict[str, Any]):
    text = t(user["lang"], "help")
    if moderation.is_staff(user):
        text += t(user["lang"], "help_admin")
    if moderation.is_owner(user):
        text += t(user["lang"], "help_owner")
    await answer(message, text)


@router.message(Command("rules"))
async def cmd_rules(message: Message, user: dict[str, Any]):
    custom = db.get("rules_text")
    await answer(message, 
        custom or t(user["lang"], "rules_default", limit=db.get_int("warn_limit") or 3)
    )


@router.message(Command("support"))
async def cmd_support(message: Message, user: dict[str, Any]):
    await answer(message, t(user["lang"], "support", owner=config.OWNER_USERNAME))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, user: dict[str, Any]):
    await answer(message, 
        t(
            user["lang"], "privacy", 
            hours=db.get_int("stub_hours") or 30,
            owner=config.OWNER_USERNAME
        )
    )


@router.message(Command("users"))
async def cmd_users(message: Message, user: dict[str, Any]):
    stats = await db.stats()
    today, mine = await db.daily_counts(user["id"])
    await answer(message, 
        t(
            user["lang"], "users",
            active=stats["active"], afk=stats["afk"], today=today, mine=mine,
        )
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message, user: dict[str, Any], bot: Bot):
    started = time.perf_counter()
    await bot.get_me()
    await answer(message, 
        t(user["lang"], "ping", ms=int((time.perf_counter() - started) * 1000))
    )


@router.message(Command("config"))
async def cmd_config(message: Message, user: dict[str, Any]):
    started = db.get_int("started_at") or int(time.time())
    await answer(message, 
        t(
            user["lang"], "config",
            uptime=timeutil.uptime_line(started),
            send_delay=db.get("send_delay"),
            slowmode=db.get("slowmode"),
            parallel=db.get("parallel_limit"),
            text=keyboards.flag(db.get_bool("echo_text")),
            doc=keyboards.flag(db.get_bool("echo_doc")),
            voice=keyboards.flag(db.get_bool("echo_voice")),
            sticker=keyboards.flag(db.get_bool("echo_sticker")),
            video=keyboards.flag(db.get_bool("echo_video")),
            photo=keyboards.flag(db.get_bool("echo_photo")),
            poll=keyboards.flag(db.get_bool("echo_poll")),
        )
    )


@router.message(Command("lang"))
async def cmd_lang(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in LANGS:
        await db.update(user["id"], lang=arg)
        await answer(message, t(arg, "lang_set", name=LANGS[arg]))
        return
    await answer(message, t(user["lang"], "lang_head"), reply_markup=keyboards.lang_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery, user: dict[str, Any]):
    code = call.data.split(":", 1)[1]
    if code in LANGS:
        await db.update(user["id"], lang=code)
        await call.message.edit_text(t(code, "lang_set", name=LANGS[code]))
    await call.answer()


# --------------------------------------------------------------------------- #
#                            профиль и настройки                               #
# --------------------------------------------------------------------------- #

@router.message(Command("profile"))
async def cmd_profile(message: Message, user: dict[str, Any]):
    lang = user["lang"]
    if moderation.is_owner(user):
        role_line = t(lang, "role_owner", prefix=moderation.prefix_of(user))
    elif user["role"] == "admin":
        role_line = t(
            lang, "role_admin",
            prefix=moderation.prefix_of(user),
            rights=await moderation.rights_summary(user),
        )
    else:
        role_line = t(lang, "role_user")

    await answer(message, 
        t(
            lang, "profile",
            name=html.escape(message.from_user.full_name),
            id=user["id"],
            clock=timeutil.fmt_clock(),
            joined=timeutil.fmt_dt_long(user["joined_at"]),
            joined_ago=timeutil.ago(user["joined_at"]),
            username=user["username_enc"] or "не сохранён",
            last=timeutil.fmt_dt_short(user["last_msg_at"]) if user["last_msg_at"] else "—",
            last_ago=timeutil.ago(user["last_msg_at"]) if user["last_msg_at"] else "—",
            msgs=user["msgs"],
            role_line=role_line,
            warns=user["warns"],
            warn_limit=db.get_int("warn_limit") or 3,
            streak=user["streak"],
        ),
        reply_markup=await profile_markup(user),
    )


@router.message(Command("tag"))
async def cmd_tag(message: Message, user: dict[str, Any]):
    await answer(message, 
        t(user["lang"], "tag_head"),
        reply_markup=keyboards.tag_kb(user, moderation.is_staff(user)),
    )


@router.message(Command("name"))
async def cmd_name(message: Message, user: dict[str, Any], command: CommandObject):
    limit = 24
    arg = (command.args or "").strip()
    if not arg:
        await db.update(user["id"], custom_name=None)
        await answer(message, 
            t(
                user["lang"], "name_head",
                name=html.escape(message.from_user.full_name), max=limit,
            )
        )
        return
    arg = arg.replace("\n", " ").replace("\r", " ").strip()
    if not arg or len(arg) > limit:
        await answer(message, t(user["lang"], "name_bad", max=limit))
        return
    await db.update(user["id"], custom_name=arg)
    await answer(message, t(user["lang"], "name_set", name=html.escape(arg)))


@router.message(Command("protect"))
async def cmd_protect(message: Message, user: dict[str, Any]):
    value = await db.toggle(user["id"], "protect")
    await answer(message, t(user["lang"], "protect_on" if value else "protect_off"))


@router.message(Command("reaction"))
async def cmd_reaction(message: Message, user: dict[str, Any]):
    value = await db.toggle(user["id"], "reaction")
    await answer(message, t(user["lang"], "reaction_toggled", v=keyboards.flag(value)))


@router.message(Command("autodel"))
async def cmd_autodel(message: Message, user: dict[str, Any]):
    await answer(message, 
        t(user["lang"], "autodel_head"), reply_markup=keyboards.autodel_kb()
    )


@router.callback_query(F.data.startswith("ad:"))
async def cb_autodel(call: CallbackQuery, user: dict[str, Any]):
    seconds = int(call.data.split(":", 1)[1])
    await db.update(user["id"], autodel=seconds)
    await call.message.edit_text(
        t(user["lang"], "autodel_set", value=timeutil.human_delta(seconds))
        if seconds else t(user["lang"], "autodel_off")
    )
    await call.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(call: CallbackQuery, user: dict[str, Any]):
    field = call.data.split(":", 1)[1]
    if field not in keyboards.ALL_TOGGLES:
        await call.answer()
        return
    user[field] = await db.toggle(user["id"], field)
    # При клике на кнопки tag/tag_link/badge в меню /tag обновляем только это меню, без возврата в профиль!
    if field in ("tag", "tag_link", "badge"):
        await call.message.edit_reply_markup(reply_markup=keyboards.tag_kb(user, moderation.is_staff(user)))
    else:
        await refresh_kb(call, user)
    await call.answer(keyboards.flag(user[field]))


@router.callback_query(F.data == "open:autodel")
async def cb_open_autodel(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(
        t(user["lang"], "autodel_head"), reply_markup=keyboards.autodel_kb()
    )
    await call.answer()


@router.callback_query(F.data == "open:name")
async def cb_open_name(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(
        t(
            user["lang"], "name_head",
            name=html.escape(call.from_user.full_name),
            max=24,
        )
    )
    await call.answer()


@router.callback_query(F.data == "t:specials")
async def cb_specials(call: CallbackQuery, user: dict[str, Any]):
    """Тумблер + гайд: что вообще делают спецсимволы."""
    user["specials"] = await db.toggle(user["id"], "specials")
    await refresh_kb(call, user)
    await call.message.answer(t(user["lang"], "specials_guide"))
    await call.answer(keyboards.flag(user["specials"]))


@router.callback_query(F.data == "open:ignore")
async def cb_open_ignore(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(await ignore_list_text(user))
    await call.answer()


@router.callback_query(F.data == "open:filter")
async def cb_open_filter(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(await filter_list_text(user))
    await call.answer()


@router.callback_query(F.data == "open:pmblock")
async def cb_open_pmblock(call: CallbackQuery, user: dict[str, Any]):
    peers = await db.pm_blocklist(user["id"])
    await call.message.answer(
        await pm_block_list_text(user),
        reply_markup=keyboards.pm_clear_kb() if peers else None,
    )
    await call.answer()


@router.callback_query(F.data == "pmclear")
async def cb_pm_clear(call: CallbackQuery, user: dict[str, Any]):
    for peer_id in await db.pm_blocklist(user["id"]):
        await db.pm_unblock(user["id"], peer_id)
    await call.message.edit_text(t(user["lang"], "pm_block_cleared"))
    await call.answer()


@router.callback_query(F.data == "close")
async def cb_close(call: CallbackQuery):
    try:
        await call.message.delete()
    except TelegramBadRequest:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# --------------------------------------------------------------------------- #
#                              архив и выход                                   #
# --------------------------------------------------------------------------- #

@router.message(Command("afk"))
async def cmd_afk(message: Message, user: dict[str, Any]):
    await db.update(user["id"], afk=1, afk_auto=0)
    await answer(message, t(user["lang"], "afk_on"))


@router.message(Command("unafk"))
async def cmd_unafk(message: Message, user: dict[str, Any]):
    await db.update(user["id"], afk=0, afk_auto=0)
    await answer(message, t(user["lang"], "afk_off"))


@router.message(Command("leave"))
async def cmd_leave(message: Message, user: dict[str, Any], command: CommandObject):
    if (command.args or "").strip() != str(user["id"]):
        await answer(message, 
            t(user["lang"], "leave_confirm", id=user["id"]),
            reply_markup=keyboards.close_kb(),
        )
        return
    await db.update(user["id"], active=0, registered=0, afk=0)
    await answer(message, t(user["lang"], "leave_done"))
    await logs.event("LEAVE", logs.who(user))


# --------------------------------------------------------------------------- #
#                                  игноры                                      #
# --------------------------------------------------------------------------- #

async def ignore_list_text(user: dict[str, Any]) -> str:
    items = await db.ignore_list(user["id"])
    if not items:
        return t(user["lang"], "ignore_empty")
    
    # Сырой Telegram ID заменен на его порядковый индекс и тип игнора (аноним/тег)
    rows = []
    for index, row in enumerate(items, 1):
        mode_label = t(user["lang"], "mode_tag") if row["mode"] == "tag" else t(user["lang"], "mode_anon")
        rows.append(f"#{index} — ({mode_label})")
        
    return t(user["lang"], "ignore_list", n=len(items), items="\n".join(rows))


@router.message(Command("ignore"))
async def cmd_ignore(message: Message, user: dict[str, Any]):
    if not message.reply_to_message:
        await answer(message, t(user["lang"], "ignore_head"))
        return
    record, target = await author_of_reply(message)
    if is_stub(record):
        await answer(message, t(user["lang"], "msg_stub"))
        return
    if not target:
        await answer(message, t(user["lang"], "target_not_found"))
        return
    if target["id"] == user["id"]:
        await answer(message, t(user["lang"], "ignore_self"))
        return
    mode = "tag" if (record and record["tagged"]) else "anon"
    if await db.is_ignored(user["id"], target["id"], mode):
        await answer(message, t(user["lang"], "ignore_exists"))
        return
    await db.ignore_add(user["id"], target["id"], mode)
    
    current_count = await db.ignore_count(user["id"])
    await answer(message, f"Пользователь(#{current_count}) был добавлен в игнор лист.")


@router.message(Command("unignore"))
async def cmd_unignore(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()
    if arg.lower() == "all":
        removed = await db.ignore_clear(user["id"])
        await answer(message, t(user["lang"], "ignore_cleared", n=removed))
        return

    target_id: Optional[int] = None
    if arg.startswith("#") and arg[1:].isdigit():
        items = await db.ignore_list(user["id"])
        index = int(arg[1:]) - 1
        if 0 <= index < len(items):
            target_id = items[index]["peer_id"]
    elif message.reply_to_message:
        _, target = await author_of_reply(message)
        target_id = target["id"] if target else None
    elif arg:
        target = await db.resolve(arg)
        target_id = target["id"] if target else None

    if target_id is None:
        await answer(message, t(user["lang"], "unignore_head"))
        return
    await db.ignore_remove(user["id"], target_id)
    await answer(message, 
        t(user["lang"], "ignore_removed", n=await db.ignore_count(user["id"]))
    )


async def pm_block_list_text(user: dict[str, Any]) -> str:
    peers = await db.pm_blocklist(user["id"])
    if not peers:
        return t(user["lang"], "pm_block_empty")
    
    # ID убраны, выводятся только индексы
    rows = []
    for index, _ in enumerate(peers, 1):
        rows.append(f"#{index}")
        
    return t(user["lang"], "pm_block_list", n=len(peers), items="\n".join(rows))


@router.message(Command("ignorelist", ignore_case=True))
async def cmd_ignorelist(message: Message, user: dict[str, Any]):
    current_count = await db.ignore_count(user["id"])
    await answer(message, f"Всего в игнор листе {current_count} пользователей.")


# --------------------------------------------------------------------------- #
#                                  фильтры                                     #
# --------------------------------------------------------------------------- #

async def filter_list_text(user: dict[str, Any]) -> str:
    items = await db.filter_list(user["id"])
    if not items:
        return t(user["lang"], "filter_empty")
    listing = "\n".join(
        f"#{index} [{row['kind']}] {html.escape(row['value'])}"
        for index, row in enumerate(items, 1)
    )
    return t(user["lang"], "filter_list", n=len(items), items=listing)


@router.message(Command("filter"))
async def cmd_filter(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()

    if message.reply_to_message and not arg:
        record = await db.message_by_copy(
            message.chat.id, message.reply_to_message.message_id
        )
        if is_stub(record):
            await answer(message, t(user["lang"], "msg_stub"))
            return
        ids = textutil.media_ids(message.reply_to_message)
        body, _ = textutil.source_text(message.reply_to_message)
        if not ids and not body.strip():
            await answer(message, t(user["lang"], "filter_need_media"))
            return
        added = 0
        for uid in ids:
            added += int(await db.filter_add(user["id"], "media", uid))
        if body.strip():
            added += int(await db.filter_add(user["id"], "word", body.strip()[:200]))
            ids = ids or [body.strip()[:40]]
        if not added:
            await answer(message, t(user["lang"], "filter_exists"))
            return
        await answer(message, 
            t(
                user["lang"], "filter_added",
                kind="media", value=ids[0], n=await db.filter_count(user["id"]),
            )
        )
        return

    if not arg:
        await answer(message, t(user["lang"], "filter_head"))
        return

    kind, value = "word", arg
    if len(arg) > 2 and arg.startswith("/") and arg.endswith("/"):
        kind, value = "regex", arg[1:-1]
        try:
            re.compile(value)
        except re.error:
            await answer(message, t(user["lang"], "filter_bad_regex"))
            return

    if not await db.filter_add(user["id"], kind, value):
        await answer(message, t(user["lang"], "filter_exists"))
        return
    await answer(message, 
        t(
            user["lang"], "filter_added",
            kind=kind, value=html.escape(value), n=await db.filter_count(user["id"]),
        )
    )


@router.message(Command("unfilter"))
async def cmd_unfilter(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()

    if arg.lower() == "all":
        await answer(message, 
            t(user["lang"], "filter_cleared", n=await db.filter_clear(user["id"]))
        )
        return

    if message.reply_to_message and not arg:
        removed = 0
        for uid in textutil.media_ids(message.reply_to_message):
            removed += await db.filter_remove(user["id"], uid)
        if not removed:
            await answer(message, t(user["lang"], "filter_not_found"))
            return
        await answer(message, 
            t(
                user["lang"], "filter_removed",
                value="media", n=await db.filter_count(user["id"]),
            )
        )
        return

    if not arg:
        await answer(message, t(user["lang"], "unfilter_head"))
        return

    value = arg[1:-1] if len(arg) > 2 and arg.startswith("/") and arg.endswith("/") else arg
    if not await db.filter_remove(user["id"], value):
        await answer(message, t(user["lang"], "filter_not_found"))
        return
    await answer(message, 
        t(
            user["lang"], "filter_removed",
            value=html.escape(value), n=await db.filter_count(user["id"]),
        )
    )


@router.message(Command("filterlist", ignore_case=True))
async def cmd_filterlist(message: Message, user: dict[str, Any]):
    await answer(message, await filter_list_text(user))


# --------------------------------------------------------------------------- #
#                             личные сообщения                                 #
# --------------------------------------------------------------------------- #

@router.message(Command("pm"))
async def cmd_pm(message: Message, user: dict[str, Any], command: CommandObject, bot: Bot):
    lang = user["lang"]
    if not message.reply_to_message:
        await answer(message, t(lang, "pm_head") + "\n\n" + t(lang, "pm_usage"))
        return
    if moderation.muted(user):
        await answer(message, moderation.mute_message(user))
        return

    record, peer = await author_of_reply(message)
    if is_stub(record) or not peer:
        await answer(message, t(lang, "msg_stub"))
        return
    if peer["id"] == user["id"]:
        await answer(message, t(lang, "report_self"))
        return
    if not peer["active"]:
        await answer(message, t(lang, "pm_fail"))
        return
    if await db.pm_blocked(peer["id"], user["id"]):
        await answer(message, t(lang, "pm_blocked"))
        return

    text = (command.args or "").strip()
    has_media = any([
        message.photo, message.video, message.animation, message.document,
        message.audio, message.voice, message.video_note, message.sticker,
    ])
    if not text and not has_media:
        await answer(message, t(lang, "pm_usage"))
        return

    # Замеряем скорость для первого сообщения
    started_time = time.perf_counter()

    # Сборка входящего сообщения получателю строго по первому шаблону из djd.txt
    pm_content = text if text else "<i>[Медиавложение]</i>"
    payload_body = (
        "Вам пришло новое Анонимное сообщение:\n"
        f"<blockquote>{pm_content}</blockquote>\n"
        "Для ответа просто ответьте на это сообщение, вы будете анонимны."
    )

    try:
        # Шлем получателю (Второй скриншот, верх)
        head = await bot.send_message(
            peer["id"],
            payload_body,
            reply_markup=keyboards.pm_kb(user["id"]),
            link_preview_options=NO_PREVIEW,
        )
        if has_media:
            await bot.copy_message(
                peer["id"], message.chat.id, message.message_id,
                caption=None, parse_mode=None,
                protect_content=bool(user["protect"]),
                reply_parameters=ReplyParameters(
                    message_id=head.message_id, allow_sending_without_reply=True
                ),
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        await db.update(peer["id"], active=0)
        await answer(message, t(lang, "pm_fail"))
        return

    # Записываем маппинг
    await db.add_pm(peer["id"], head.message_id, user["id"])
    
    # Резолвим под удаление
    ref_record = await db.add_message(user["id"], message.message_id)
    await db.add_copies(ref_record["ref"], [(peer["id"], head.message_id)])

    elapsed = time.perf_counter() - started_time
    ms_formatted = timeutil.human_ms(elapsed)

    # Отправителю выводим замер скорости отправки (Первый скриншот, верх)
    await answer(
        message,
        f"Твоё сообщение успешно отправлено за {ms_formatted}",
        reply_markup=keyboards.sent_kb(ref_record["token"])
    )


@router.message(Command("pmblocks", "blocks"))
async def cmd_pmblocks(message: Message, user: dict[str, Any]):
    await answer(message, await pm_block_list_text(user))


@router.message(Command("pmunblock"))
async def cmd_pmunblock(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    peers = await db.pm_blocklist(user["id"])
    if arg.lower() == "all":
        for peer_id in peers:
            await db.pm_unblock(user["id"], peer_id)
        await answer(message, t(user["lang"], "pm_block_cleared"))
        return
    if not arg.isdigit() or not (1 <= int(arg) <= len(peers)):
        await answer(message, await pm_block_list_text(user))
        return
    await db.pm_unblock(user["id"], peers[int(arg) - 1])
    await answer(message, t(user["lang"], "pm_unblock_done"))


@router.callback_query(F.data.startswith("pmb:"))
async def cb_pm_block(call: CallbackQuery, user: dict[str, Any]):
    await db.pm_block(user["id"], int(call.data.split(":", 1)[1]))
    await call.answer(t(user["lang"], "pm_block_done"), show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


# --------------------------------------------------------------------------- #
#                                  репорты                                     #
# --------------------------------------------------------------------------- #

@router.message(Command("report"))
async def cmd_report(message: Message, user: dict[str, Any], command: CommandObject, bot: Bot):
    lang = user["lang"]
    if moderation.muted(user):
        await answer(message, t(lang, "report_muted"))
        return
    if not message.reply_to_message:
        await answer(message, t(lang, "report_usage"))
        return

    left = user["last_report"] + db.get_int("report_cooldown") - int(time.time())
    if left > 0:
        await answer(message, t(lang, "report_cd", left=timeutil.human_delta(left)))
        return

    record, target = await author_of_reply(message)
    if not record or record["stub"] or record["deleted"]:
        await answer(message, t(lang, "msg_stub"))
        return
    if record["author_id"] == user["id"]:
        await answer(message, t(lang, "report_self"))
        return

    # Запрещаем жаловаться на овнера или админов
    if target and target.get("role") in ("admin", "owner"):
        await answer(message, t(lang, "no_rights"))
        return

    reason = (command.args or "").strip() or "—"
    await db.add_report(user["id"], record["author_id"], record["ref"], reason)
    await db.update(user["id"], last_report=int(time.time()))

    header = t(
        lang, "report_admin",
        target=str(target["id"]) if target else "?",
        target_id=record["author_id"],
        author=str(user["id"]),
        author_id=user["id"],
        reason=html.escape(reason),
    )
    for member in await db.staff():
        copy_id = await db.copy_in_chat(record["ref"], member["id"])
        try:
            if copy_id:
                await bot.send_message(
                    member["id"], header,
                    reply_parameters=ReplyParameters(
                        message_id=copy_id, allow_sending_without_reply=True
                    ),
                    link_preview_options=NO_PREVIEW,
                )
            else:
                shown = await bot.send_message(
                    member["id"], header, link_preview_options=NO_PREVIEW
                )
                await bot.copy_message(
                    member["id"], record["author_id"], record["author_msg_id"],
                    reply_parameters=ReplyParameters(
                        message_id=shown.message_id, allow_sending_without_reply=True
                    ),
                )
        except (TelegramForbiddenError, TelegramBadRequest):
            continue

    await answer(message, t(lang, "report_sent"))
    await logs.event("REPORT", header)


# --------------------------------------------------------------------------- #
#                      удаление и редактирование своего эхо                    #
# --------------------------------------------------------------------------- #

async def do_delete(call: CallbackQuery, user: dict[str, Any], bot: Bot, token: str):
    record = await db.message_by_token(token)
    if not record or record["author_id"] != user["id"]:
        await call.answer(t(user["lang"], "not_your_msg"), show_alert=True)
        return
    if record["deleted"]:
        await call.answer(t(user["lang"], "msg_stub"), show_alert=True)
        return
    removed = await moderation.delete_message(bot, record["ref"])
    await logs.event("DELETE", t("ru", "log_delete", who=logs.who(user), count=removed))
    await call.answer(t(user["lang"], "deleted_ok", count=removed))


@router.callback_query(F.data.startswith("d:"))
async def cb_delete(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    token = call.data.split(":", 1)[1]
    if not user["del_warning"]:
        await do_delete(call, user, bot, token)
        return
    await call.message.edit_reply_markup(reply_markup=keyboards.delete_confirm_kb(token))
    await call.answer(t(user["lang"], "delete_confirm"))


@router.callback_query(F.data.startswith("dy:"))
async def cb_delete_yes(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    await do_delete(call, user, bot, call.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("dn:"))
async def cb_delete_no(call: CallbackQuery):
    await call.message.edit_reply_markup(
        reply_markup=keyboards.sent_kb(call.data.split(":", 1)[1])
    )
    await call.answer()


@router.edited_message(F.chat.type == "private")
async def on_edited(message: Message, user: dict[str, Any], bot: Bot):
    record = await db.message_by_author_msg(user["id"], message.message_id)
    if not record or record["deleted"]:
        return
    if record["stub"]:
        await answer(message, t(user["lang"], "msg_stub"))
        return

    raw_text, raw_entities = textutil.source_text(message)
    is_text = message.text is not None

    if user["autoedit"]:
        changed = await delivery.propagate_edit(
            bot, record["ref"], user, raw_text, raw_entities, is_text,
            echo_markup(message, user),
            echo_markup(message, user, with_delete=record["token"]),
        )
        await answer(message, t(user["lang"], "edited_ok", count=changed))
        return

    PENDING_EDITS[record["token"]] = (raw_text, raw_entities, is_text)
    await answer(message, 
        t(user["lang"], "edit_confirm"),
        reply_markup=keyboards.edit_confirm_kb(record["token"]),
    )


@router.callback_query(F.data.startswith("ey:"))
async def cb_edit_confirm(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    token = call.data.split(":", 1)[1]
    record = await db.message_by_token(token)
    payload = PENDING_EDITS.pop(token, None)
    if not record or record["author_id"] != user["id"] or not payload:
        await call.answer(t(user["lang"], "not_your_msg"), show_alert=True)
        return
    raw_text, raw_entities, is_text = payload
    markup = keyboards.echo_kb(
        user,
        display_name=call.from_user.full_name,
        username=call.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
    )
    self_markup = keyboards.echo_kb(
        user,
        display_name=call.from_user.full_name,
        username=call.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
        with_delete=token,
    )
    changed = await delivery.propagate_edit(
        bot, record["ref"], user, raw_text, raw_entities, is_text, markup, self_markup
    )
    await call.message.edit_text(t(user["lang"], "edited_ok", count=changed))
    await call.answer()


# --------------------------------------------------------------------------- #
#                                   реакции                                    #
# --------------------------------------------------------------------------- #

@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, bot: Bot):
    record = await db.message_by_copy(event.chat.id, event.message_id)
    if not record or record["deleted"]:
        return

    reactor = await db.get_user(event.chat.id)
    if reactor and moderation.muted(reactor):
        return

    author = await db.get_user(record["author_id"])
    if not author or not author["reaction"] or not author["active"]:
        return

    reaction = list(event.new_reaction or [])
    if not reaction:
        return

    targets = [(author["id"], record["author_msg_id"])]
    targets += list(await db.copies(record["ref"]))

    semaphore = asyncio.Semaphore(max(1, db.get_int("parallel_limit")))

    async def mirror(chat_id: int, msg_id: int) -> None:
        async with semaphore:
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id, message_id=msg_id, reaction=reaction
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await bot.set_message_reaction(
                        chat_id=chat_id, message_id=msg_id, reaction=reaction
                    )
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

    await asyncio.gather(*(mirror(chat_id, msg_id) for chat_id, msg_id in targets))


# --------------------------------------------------------------------------- #
#                        ЦВЕТНЫЕ КНОПКИ ДЛЯ ТЕГА                             #
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == "open:tag_colors")
async def cb_open_tag_colors(call: CallbackQuery, user: dict[str, Any]):
    """Открывает меню выбора цвета."""
    current_color = user.get("tag_color") or "default"
    await call.message.edit_text(
        "<b>Вы можете выбрать цвета для тега. Он будет использоваться для отправки сообщений, если у вас включен тег.</b>",
        reply_markup=keyboards.tag_colors_kb(current_color)
    )
    await call.answer()


@router.callback_query(F.data.startswith("set_color:"))
async def cb_set_tag_color(call: CallbackQuery, user: dict[str, Any]):
    """Сохраняет выбранный цвет в БД и обновляет меню выбора цвета."""
    color = call.data.split(":", 1)[1]
    await db.update(user["id"], tag_color=color)
    user["tag_color"] = color  # обновляем в памяти
    
    await call.message.edit_reply_markup(reply_markup=keyboards.tag_colors_kb(color))
    await call.answer(f"Выбран цвет: {color}")


@router.callback_query(F.data == "t:back_to_tag")
async def cb_back_to_tag(call: CallbackQuery, user: dict[str, Any]):
    """Возвращает пользователя обратно в главное меню /tag."""
    await call.message.edit_text(
        t(user["lang"], "tag_head"),
        reply_markup=keyboards.tag_kb(user, moderation.is_staff(user))
    )
    await call.answer()


# --------------------------------------------------------------------------- #
#                        ПРЕМИУМ ЭМОДЗИ ДЛЯ ТЕГА                             #
# --------------------------------------------------------------------------- #

@router.message(Command("emoji"))
async def cmd_emoji(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()
    
    # Если аргументов нет
    if not arg:
        if user.get("custom_emoji_id"):
            await db.update(user["id"], custom_emoji_id=None)
            await answer(message, t(user["lang"], "emoji_off"))
        else:
            try:
                await answer(message, t(user["lang"], "emoji_head"), parse_mode="HTML")
            except TelegramBadRequest as exc:
                if "DOCUMENT_INVALID" in str(exc):
                    await answer(message, "Не валидный/битый ID. Попробуйте другой или вставьте правильно")
                else:
                    raise
        return

    emoji_id = None
    # 1. Извлекаем ID из отправленного премиум-эмодзи
    if message.entities:
        for entity in message.entities:
            if entity.type == "custom_emoji":
                emoji_id = entity.custom_emoji_id
                break

    # 2. Извлекаем ID из переданного аргумента
    if not emoji_id and arg.isdigit():
        emoji_id = arg

    if not emoji_id:
        try:
            await answer(message, t(user["lang"], "emoji_bad") + "\n\n" + t(user["lang"], "emoji_head"), parse_mode="HTML")
        except TelegramBadRequest as exc:
            if "DOCUMENT_INVALID" in str(exc):
                await answer(message, "Не валидный/битый ID. Попробуйте другой или вставьте правильно")
            else:
                raise
        return

    # Записываем ID в базу данных
    await db.update(user["id"], custom_emoji_id=emoji_id)

    # 3. Выводим ответ с живым премиум-эмодзи через HTML-тег <tg-emoji>
    try:
        response_text = (
            f"{t(user['lang'], 'emoji_set')}\n"
            f"Установлен эмодзи: <tg-emoji emoji-id=\"{emoji_id}\">✨</tg-emoji>"
        )
        await answer(message, response_text, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "DOCUMENT_INVALID" in str(exc):
            await db.update(user["id"], custom_emoji_id=None)
            await answer(message, "Не валидный/битый ID. Попробуйте другой или вставьте правильно")
        else:
            raise


# --------------------------------------------------------------------------- #
#                                    ЭХО                                       #
# --------------------------------------------------------------------------- #

@router.message(F.chat.type == "private")
async def echo(message: Message, user: dict[str, Any], bot: Bot):
    try:
        await _echo(message, user, bot)
    except Exception as exc:  # noqa: BLE001
        log.exception("echo failed for %s: %s", user["id"], exc)
        await logs.event("ERROR", f"{logs.who(user)}\n<code>{logs.esc(exc)}</code>")
        try:
            await answer(message, f"Сбой при отправке: {exc}", parse_mode=None)
        except Exception:  # noqa: BLE001
            pass


async def _echo(message: Message, user: dict[str, Any], bot: Bot):
    lang = user["lang"]

    if message.text and message.text.startswith("/"):
        await answer(message, t(lang, "unknown_cmd"))
        return

    if not user["registered"]:
        await answer(message, 
            t(lang, "greet", name=html.escape(message.from_user.full_name)),
            reply_markup=keyboards.confirm_start_kb(),
        )
        return

    if moderation.muted(user):
        await answer(message, moderation.mute_message(user))
        return

    if user["afk"]:
        note = t(lang, "afk_restored")
        if user["afk_auto"]:
            note += t(lang, "tag_disabled_safety")
        await db.update(user["id"], afk=0, afk_auto=0)
        await answer(message, note)
        return

    if not user["active"]:
        await db.update(user["id"], active=1)

    # ----------------------------------------------------------------------- #
    #                  ОБРАБОТКА ОТВЕТА НА АНОНИМНОЕ ЛС                      #
    # ----------------------------------------------------------------------- #
    if message.reply_to_message:
        # Проверяем, было ли сообщение, на которое отвечают, пришедшим ЛС
        peer_id = await db.get_pm(message.chat.id, message.reply_to_message.message_id)
        if peer_id:
            reply_text = message.text or ""
            has_media = any([
                message.photo, message.video, message.animation, message.document,
                message.audio, message.voice, message.video_note, message.sticker,
            ])
            if not reply_text and not has_media:
                return

            payload_body = ""
            
            # Находим исходный текст анонимного сообщения (Второй скриншот, верх)
            # Чтобы вставить его в цитату <blockquote> для отправителя ЛС
            # НА ЭТОТ ОТВЕТ МЫ СТАВИМ ЦИТАТУ ТЕКСТА ИСХОДНОГО ЛС (aa 99)!
            source_content, _ = textutil.source_text(message.reply_to_message)
            if source_content:
                # Очищаем заголовок и инструкцию, чтобы остался чистый текст ЛС
                if "Вам пришло новое Анонимное сообщение:\n" in source_content:
                    source_content = source_content.replace("Вам пришло новое Анонимное сообщение:\n", "")
                if "\nДля ответа просто ответьте на это сообщение, вы будете анонимны." in source_content:
                    source_content = source_content.replace("\nДля ответа просто ответьте на это сообщение, вы будете анонимны.", "")
                
                source_content_cut = source_content[:120] + "..." if len(source_content) > 120 else source_content
                payload_body += f"<blockquote>{source_content_cut}</blockquote>\n"

            # Текст ответа (Первый скриншот, низ)
            # ОЧИЩЕНА ИСТОРИЯ НАКОПЛЕНИЯ СТАРЫХ ОТВЕТОВ В СИСТЕМНОЙ ПЛАШКЕ!
            payload_body += "Вам ответили на Ваше сообщение\n"
            if reply_text:
                payload_body += reply_text
            elif has_media:
                payload_body += "<i>[Медиавложение]</i>"

            try:
                # Отправляем ответ автору ЛС (Первый скриншот, низ)
                head = await bot.send_message(
                    peer_id,
                    payload_body,
                    reply_markup=keyboards.pm_kb(user["id"]),
                    link_preview_options=NO_PREVIEW,
                )
                if has_media:
                    await bot.copy_message(
                        peer_id, message.chat.id, message.message_id,
                        caption=None, parse_mode=None,
                        protect_content=bool(user["protect"]),
                        reply_parameters=ReplyParameters(
                            message_id=head.message_id, allow_sending_without_reply=True
                        ),
                    )
            except (TelegramForbiddenError, TelegramBadRequest):
                await answer(message, t(lang, "pm_fail"))
                return

            # Сохраняем маппинг
            await db.add_pm(peer_id, head.message_id, user["id"])
            
            # Добавляем в messages для возможности удалить отправленный ответ
            ref_record = await db.add_message(user["id"], message.message_id)
            await db.add_copies(ref_record["ref"], [(peer_id, head.message_id)])

            # Получателю, который ответил, выводится системная плашка "Пользователь получил ответ." (Второй скриншот, низ)
            await answer(
                message,
                "<b>Пользователь получил ответ.</b>",
                reply_markup=keyboards.sent_kb(ref_record["token"])
            )
            return

    # ----------------------------------------------------------------------- #
    #                       СТАНДАРТНАЯ ОТПРАВКА ЭХО                          #
    # ----------------------------------------------------------------------- #
    now_ts = int(time.time())
    slowmode = db.get_int("slowmode")

    # Жёсткий фикс спама обрывками: проверяем КД
    if slowmode and (now_ts - user["last_msg_at"] < slowmode) and slowmode_applies(user):
        left = slowmode - (now_ts - user["last_msg_at"])
        # Предупреждение шлем максимум раз в секунду, чтобы сам бот не засрал чат
        if now_ts - user["last_msg_at"] >= 1:
            await answer(message, t(lang, "slowmode", left=left))
        return

    # Записываем таймштамп СРАЗУ, дабы отсечь все параллельно летящие куски текста
    await db.update(user["id"], last_msg_at=now_ts)
    user["last_msg_at"] = now_ts

    kind = delivery.message_kind(message)
    if user["media_block"] and kind != "text":
        await answer(message, t(lang, "media_blocked"))
        return
    if not delivery.kind_allowed(kind):
        await answer(message, t(lang, "type_disabled"))
        return

    batch = await delivery.albums.collect(message)
    if batch is None:
        return

    digest = textutil.content_hash(batch)
    if db.get_bool("no_duplicates") and digest == user["last_hash"]:
        window = db.get_int("duplicate_window") or 600
        if int(time.time()) - (user["last_hash_at"] or 0) < window:
            return

    await sync_username(message, user)

    reply_ref: Optional[int] = None
    if message.reply_to_message:
        parent = await db.find_message(message.chat.id, message.reply_to_message.message_id)
        if parent:
            if parent["stub"] or parent["deleted"]:
                await answer(message, t(lang, "msg_stub"))
                return
            reply_ref = parent["ref"]

    flags: dict = {}
    if user["specials"] and db.get_bool("specials_enabled"):
        raw_text, raw_ents = textutil.source_text(batch[0])
        cleaned, flags, cut = textutil.parse_specials(raw_text)
        if cut:
            text_only, ents = textutil.cut_prefix(cleaned, raw_ents, cut)
            flags["text"] = text_only
            flags["entities"] = ents

    record = await db.add_message(user["id"], batch[0].message_id, reply_ref)
    await db.mark_message(record["ref"], tagged=1 if user["tag"] else 0)
    album = len(batch) > 1
    tagged = bool(user["tag"]) and not flags.get("notag")
    view = dict(user, tag=1 if tagged else 0)
    markup = None if album else echo_markup(message, view)

    sending = asyncio.create_task(
        delivery.broadcast(bot, view, batch, record["ref"], reply_ref, markup, flags)
    )
    _AUTODEL_TASKS.add(sending)
    sending.add_done_callback(_AUTODEL_TASKS.discard)

    report = await answer(message, 
        t(lang, "sending"), link_preview_options=NO_PREVIEW, parse_mode=None
    )

    self_ids = await delivery.deliver_self(
        bot, view, batch, record["ref"], reply_ref,
        None if album else echo_markup(message, view, with_delete=record["token"]),
        flags,
    )

    await db.update(
        user["id"],
        msgs=user["msgs"] + 1,
        last_hash=digest,
        last_hash_at=int(time.time()),
    )
    await db.bump_daily(user["id"])

    result = await sending
    try:
        await report.edit_text(
            t(lang, "nobody") if result.total == 0 else delivery.report_text(lang, result),
            link_preview_options=NO_PREVIEW,
            parse_mode=None,
            reply_markup=(
                keyboards.sent_kb(record["token"])
                if not self_ids and not album else None
            ),
        )
    except TelegramBadRequest:
        pass

    delay = flags.get("autodel", 0) * 60 or user["autodel"]
    if delay:
        await schedule_autodelete(bot, record["ref"], delay)