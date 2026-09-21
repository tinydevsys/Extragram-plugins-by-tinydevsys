# -*- coding: utf-8 -*-
"""
Send as Accounts — плагин для exteraGram.

Позволяет писать от имени других (личных) аккаунтов так же, как сейчас
можно писать от лица привязанного канала в группах:

* список «Отправить от…» дополняется личными аккаунтами (те, что
  залогинены в приложении), порядок и скрытие настраиваются;
* в группах сообщения уходят от выбранного аккаунта (отправляются
  через его соединение), выбор можно запомнить на чат;
* в личных чатах и ботах сообщение отправляется от выбранного
  аккаунта, а затем предлагается переключить приложение на него
  (или переключается сразу, если выбран режим полной смены);
* долгое нажатие на аккаунт — полная смена приложения на этот
  аккаунт (независимо от режима, если включено в настройках);
* аккаунты, которых нет в чате, показаны серыми со статусами
  «не в чате» / «исключён» / «забанен»; по нажатию предлагается
  вступить (если у чата есть публичная ссылка).

Требования: exteraGram >= 12.5.1, SDK >= 1.4.4.3, минимум два
залогиненных аккаунта.
"""

import threading
import time
import traceback
from typing import Any, List

from base_plugin import (
    BasePlugin,
    MethodHook,
    MenuItemData,
    MenuItemType,
)
from hook_utils import find_class, get_private_field
from android_utils import OnClickListener, log, run_on_ui_thread
from client_utils import get_last_fragment
from ui.bulletin import BulletinHelper
from ui.alert import AlertDialogBuilder
from ui.settings import Divider, Header, Input, Selector, Switch, Text

from extera_utils.classes import Base, java_subclass, jfield, joverride

from java import jarray, jclass
from java.lang import Boolean as JBoolean
from java.lang import Integer as JInteger
from java.lang import Long as JLong
from java.lang import Object as JObject

from android.view import Gravity, View as JView

from org.telegram.messenger import (
    AccountInstance,
    AndroidUtilities,
    ChatObject,
    DialogObject,
    MessagesController,
    NotificationCenter,
    R as R_tg,
    UserConfig,
    UserObject,
)
from org.telegram.tgnet import ConnectionsManager, TLRPC
from org.telegram.ui import LaunchActivity
from org.telegram.ui.Components import RecyclerListView, SenderSelectPopup


# ---------------------------------------------------------------------------
# Метаданные плагина (парсятся статически — только простые константы)
# ---------------------------------------------------------------------------

__id__ = "send_as_accounts"
__name__ = "Send as Accounts"
__description__ = (
    "Переписка от других аккаунтов: выбор личных аккаунтов в списке «Отправить от…» "
    "(как отправка от канала в группах). В группах сообщения уходят от выбранного "
    "аккаунта, в ЛС и ботах — с предложением переключиться. Долгое нажатие — полная "
    "смена аккаунта."
)
__author__ = "tinydevsys"
__version__ = "1.0.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.4.3"


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

MODE_SWITCH = 0   # переключение отправителя (группы и ЛС)
MODE_FULL = 1     # полная смена приложения
MODE_HYBRID = 2   # группы — переключение отправителя; ЛС и боты — полная смена
MODE_NAMES = [
    "Переключение отправителя",
    "Полная смена приложения",
    "Гибридный",
]

VIEW_TAG_WRAPPED = "send_as_accounts_wrapped"

# Регистр состояний для сгенерированных Java-прокси (адаптер / слушатели).
# token (jfield) -> dict с данными.
STATE_REGISTRY = {}


def kind_text(kind):
    return {
        "kicked": "исключён",
        "banned": "забанен",
        "out": "не в чате",
        "unknown": "не в чате",
    }.get(kind, "аккаунт")


def _is_encrypted(peer):
    try:
        return bool(DialogObject.isEncryptedDialog(peer))
    except Exception:
        return False


def _is_banned(chat):
    """Блокировка (view_messages) в любом из слоёв TL-схемы."""
    try:
        if chat.banned_rights is not None and chat.banned_rights.view_messages:
            return True
    except Exception:
        pass
    try:
        newer = getattr(chat, "banned_rights_layer92", None)
        if newer is not None and newer.view_messages:
            return True
    except Exception:
        pass
    return False


def _jclass(name):
    """Объект java.lang.Class для рефлексии (getDeclaredMethod и т.п.).

    ВАЖНО: в этой версии SDK find_class возвращает Python-type обёртку,
    у которой НЕТ методов рефлексии — только jclass даёт настоящий
    объект java.lang.Class.
    """
    try:
        return jclass(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Java-прокси (создаются DexMaker'ом в runtime)
# ---------------------------------------------------------------------------

@java_subclass(RecyclerListView.SelectionAdapter)
class SenderAdapter(Base):
    """Адаптер списка «Отправить от…»: оригинальные пункты + аккаунты."""

    token = jfield("int", default=-1)

    def _st(self):
        return STATE_REGISTRY.get(self.token)

    @joverride()
    def getItemCount(self):
        st = self._st()
        if st is None:
            return 0
        return st["orig_count"] + len(st["accounts"])

    @joverride()
    def isEnabled(self, holder):
        return True

    @joverride()
    def getItemViewType(self, position):
        st = self._st()
        if st is None:
            return 0
        return 0 if position < st["orig_count"] else 1

    @joverride()
    def onCreateViewHolder(self, parent, viewType):
        st = self._st()
        if st is None:
            return None
        if viewType == 0 and st["orig_count"] > 0 and st["orig_adapter"] is not None:
            return st["orig_adapter"].onCreateViewHolder(parent, 0)
        context = parent.getContext() if parent is not None else None
        return RecyclerListView.Holder(SenderSelectPopup.SenderView(context))

    @joverride()
    def onBindViewHolder(self, holder, position):
        st = self._st()
        if st is None:
            return
        if position < st["orig_count"]:
            st["orig_adapter"].onBindViewHolder(holder, st["orig_positions"][position])
        else:
            acc = st["accounts"][position - st["orig_count"]]
            st["plugin"]._bind_account_row(holder.itemView, acc, st["peer"])


@java_subclass(JObject, RecyclerListView.OnItemClickListener)
class ClickProxy(Base):
    """Обёртка обработчика нажатий списка: оригинальные пункты + аккаунты."""

    token = jfield("int", default=-1)

    @joverride()
    def onItemClick(self, view, position):
        st = STATE_REGISTRY.get(self.token)
        if st is None:
            return
        plugin = st["plugin"]
        try:
            if position < st["orig_count"]:
                # Сначала сбрасываем сохранённого аккаунта, чтобы оригинальный
                # поток (updateSendAsButton) уже не подменял аватар обратно.
                plugin._on_original_peer_selected(st["peer"])
                if st["orig_listener"] is not None:
                    st["orig_listener"].onItemClick(view, st["orig_positions"][position])
            else:
                acc = st["accounts"][position - st["orig_count"]]
                plugin._on_account_row_tapped(st["peer"], acc, st["popup"], view)
        except Exception:
            log(traceback.format_exc())


@java_subclass(JObject, RecyclerListView.OnItemLongClickListener)
class LongClickProxy(Base):
    """Долгое нажатие на аккаунт — полная смена приложения."""

    token = jfield("int", default=-1)

    @joverride()
    def onItemClick(self, view, position):
        st = STATE_REGISTRY.get(self.token)
        if st is None:
            return False
        plugin = st["plugin"]
        try:
            if position >= st["orig_count"] and plugin.get_setting("long_press_switch", True):
                acc = st["accounts"][position - st["orig_count"]]
                if st["popup"] is not None:
                    try:
                        st["popup"].dismiss()
                    except Exception:
                        pass
                plugin._switch_app_to(acc, reason="long_press")
                return True
        except Exception:
            log(traceback.format_exc())
        return False


@java_subclass(JObject, SenderSelectPopup.OnSelectCallback)
class SelectCallbackProxy(Base):
    """Заглушка OnSelectCallback для собственных попапов (без оригинальных пунктов)."""

    @joverride()
    def onPeerSelected(self, recyclerView, senderView, peer):
        # Для попапов плагина оригинальных (канальных) пунктов нет,
        # аккаунты обрабатывает ClickProxy.
        pass


_join_callback_cls = None


def _make_join_callback_class():
    """Ленивая генерация прокси RequestCallback для channels.joinChannel."""
    global _join_callback_cls
    if _join_callback_cls is not None:
        return _join_callback_cls
    iface = find_class("org.telegram.tgnet.RequestCallback")
    if iface is None:
        return None

    @java_subclass(JObject, iface)
    class _JoinCallback(Base):
        token = jfield("int", default=-1)

        @joverride()
        def run(self, response, error):
            st = STATE_REGISTRY.get(self.token)
            if st is not None and st.get("on_result") is not None:
                st["on_result"](response, error)

    _join_callback_cls = _JoinCallback
    return _JoinCallback


# ---------------------------------------------------------------------------
# Обработчики Xposed-хуков
# ---------------------------------------------------------------------------

class PopupCtorHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        self.plugin._on_popup_created(param)


class SenderViewCreateHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        self.plugin._on_sender_view_created(param)


class UpdateSendAsHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        self.plugin._on_update_send_as_after(param)


class SendRedirectHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def before_hooked_method(self, param):
        self.plugin._on_send_before(param)

    def after_hooked_method(self, param):
        self.plugin._on_send_after(param)


# ---------------------------------------------------------------------------
# Плагин
# ---------------------------------------------------------------------------

class SendAsAccountsPlugin(BasePlugin):

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def on_plugin_load(self):
        self._state_seq = 0
        self._mem_senders = {}
        self._last_redirect = None
        self._pending_popup_peer = None
        self._tls = threading.local()
        self._hooks_ok = 0
        self._hooks_total = 4
        self._warned = set()
        self._diag_log = []

        try:
            self.menu_item_id = self._add_menu_item()
        except Exception:
            log(traceback.format_exc())
            self._diag("menu item: " + traceback.format_exc().strip().splitlines()[-1])
            self.menu_item_id = None

        self._hook_popup()
        self._hook_sender_view()
        self._hook_update_send_as()
        self._hook_send()
        self.log(
            "Send as Accounts: loaded, hooks {}/{}".format(self._hooks_ok, self._hooks_total)
        )
        self._diag(
            "loaded: hooks {}/{} accounts={}".format(
                self._hooks_ok,
                self._hooks_total,
                len([a for a in range(UserConfig.MAX_ACCOUNT_COUNT) if UserConfig.isValidAccount(a)]),
            )
        )

    def on_plugin_unload(self):
        STATE_REGISTRY.clear()
        self.log("Send as Accounts: unloaded")

    # ------------------------------------------------------------------
    # Диагностика (видимая пользователю)
    # ------------------------------------------------------------------

    def _diag(self, msg):
        try:
            self._diag_log.append(
                "{} {}".format(time.strftime("%H:%M:%S"), str(msg)[:300])
            )
            if len(self._diag_log) > 25:
                self._diag_log = self._diag_log[-25:]
        except Exception:
            pass

    def _warn_once(self, key, text):
        """Bulletin об ошибке — не чаще раза за сессию на каждый key."""
        try:
            if key in self._warned:
                return
            self._warned.add(key)
            run_on_ui_thread(lambda: BulletinHelper.show_error(text))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Регистрация хуков
    # ------------------------------------------------------------------

    def _add_menu_item(self):
        menu_id = self.add_menu_item(
            MenuItemData(
                menu_type=MenuItemType.CHAT_ACTION_MENU,
                text="Отправлять от…",
                icon="msg_user_search",
                subtext="Выбор аккаунта отправителя",
                priority=80,
                on_click=self._on_chat_action_menu,
            )
        )
        self.add_menu_item(
            MenuItemData(
                menu_type=MenuItemType.CHAT_ACTION_MENU,
                text="Send as Accounts: диагностика",
                icon="msg_settings",
                subtext="Состояние плагина",
                priority=1,
                on_click=lambda _ctx: self._show_diag(),
            )
        )
        return menu_id

    def _hook_popup(self):
        try:
            cls = _jclass("org.telegram.ui.Components.SenderSelectPopup")
            if cls is None:
                self.log("Send as Accounts: SenderSelectPopup class not found")
                self._diag("popup hook: SenderSelectPopup class not found")
                return
            ctor = cls.getDeclaredConstructor(
                _jclass("android.content.Context"),
                _jclass("org.telegram.ui.ChatActivity"),
                _jclass("org.telegram.messenger.MessagesController"),
                _jclass("org.telegram.tgnet.TLRPC$ChatFull"),
                _jclass("org.telegram.tgnet.TLRPC$TL_channels_sendAsPeers"),
                _jclass("org.telegram.ui.Components.SenderSelectPopup$OnSelectCallback"),
            )
            ctor.setAccessible(True)
            self.hook_method(ctor, PopupCtorHook(self))
            self._hooks_ok += 1
            self._diag("popup hook: ok")
        except Exception:
            log(traceback.format_exc())
            self._diag("popup hook: " + traceback.format_exc().strip().splitlines()[-1])

    def _hook_sender_view(self):
        try:
            cls = _jclass("org.telegram.ui.Components.ChatActivityEnterView")
            if cls is None:
                self._diag("sender_view hook: class not found")
                return
            m = cls.getDeclaredMethod("createSenderSelectView")
            m.setAccessible(True)
            self.hook_method(m, SenderViewCreateHook(self))
            self._hooks_ok += 1
            self._diag("sender_view hook: ok")
        except Exception:
            log(traceback.format_exc())
            self._diag("sender_view hook: " + traceback.format_exc().strip().splitlines()[-1])

    def _hook_update_send_as(self):
        try:
            cls = _jclass("org.telegram.ui.Components.ChatActivityEnterView")
            if cls is None:
                self._diag("update_send_as hook: class not found")
                return
            m = cls.getDeclaredMethod("updateSendAsButton", JBoolean.TYPE)
            m.setAccessible(True)
            self.hook_method(m, UpdateSendAsHook(self))
            self._hooks_ok += 1
            self._diag("update_send_as hook: ok")
        except Exception:
            log(traceback.format_exc())
            self._diag("update_send_as hook: " + traceback.format_exc().strip().splitlines()[-1])

    def _hook_send(self):
        try:
            cls = _jclass("org.telegram.messenger.SendMessagesHelper")
            if cls is None:
                self.log("Send as Accounts: SendMessagesHelper class not found")
                self._diag("send hook: SendMessagesHelper class not found")
                return
            hooked = 0
            for m in cls.getDeclaredMethods():
                if m.getName() != "sendMessage":
                    continue
                params = m.getParameterTypes()
                if len(params) == 27:
                    # Основной путь отправки (текст/медиа/файлы/опросы).
                    m.setAccessible(True)
                    self.hook_method(m, SendRedirectHook(self))
                    hooked += 1
                elif len(params) == 7 and params[1].getName() == "long":
                    # Пересылка сообщений.
                    m.setAccessible(True)
                    self.hook_method(m, SendRedirectHook(self))
                    hooked += 1
            if hooked:
                self._hooks_ok += 1
                self._diag("send hook: ok ({} methods)".format(hooked))
            else:
                self._diag("send hook: no sendMessage overloads found")
        except Exception:
            log(traceback.format_exc())
            self._diag("send hook: " + traceback.format_exc().strip().splitlines()[-1])

    # ------------------------------------------------------------------
    # Состояние и настройки
    # ------------------------------------------------------------------

    def _register_state(self, state):
        self._state_seq = getattr(self, "_state_seq", 0) + 1
        STATE_REGISTRY[self._state_seq] = state
        if len(STATE_REGISTRY) > 32:
            for k in list(STATE_REGISTRY.keys())[: max(0, len(STATE_REGISTRY) - 32)]:
                STATE_REGISTRY.pop(k, None)
        return self._state_seq

    def _sender_map(self):
        if self.get_setting("remember_sender", True):
            return dict(self.get_setting("chat_senders", {}) or {})
        return dict(getattr(self, "_mem_senders", {}))

    def get_sender_account(self, peer):
        """Аккаунт-отправитель, выбранный для peer (dialog id), или None."""
        if peer is None:
            return None
        try:
            acc = self._sender_map().get(str(peer))
        except Exception:
            return None
        if acc is None or not UserConfig.isValidAccount(acc):
            return None
        return acc

    def _get_order(self):
        accounts = [
            a for a in range(UserConfig.MAX_ACCOUNT_COUNT)
            if UserConfig.isValidAccount(a)
        ]
        saved = self.get_setting("account_order", []) or []
        order = [a for a in saved if a in accounts]
        for a in accounts:
            if a not in order:
                order.append(a)
        return order

    def _get_accounts_for_list(self):
        hidden = self.get_setting("hidden_accounts", {}) or {}
        return [a for a in self._get_order() if str(a) not in hidden]

    def _account_name(self, acc):
        try:
            user = UserConfig.getInstance(acc).getCurrentUser()
        except Exception:
            user = None
        if user is None:
            return "Аккаунт {}".format(acc)
        try:
            name = UserObject.getUserName(user)
            return name if name else "Аккаунт {}".format(acc)
        except Exception:
            return "Аккаунт {}".format(acc)

    def _account_user(self, acc):
        try:
            return UserConfig.getInstance(acc).getCurrentUser()
        except Exception:
            return None

    def _account_status(self, peer, acc):
        """(kind, text): member/kicked/banned/out/unknown."""
        if peer is None or peer > 0:
            return ("member", "аккаунт")
        try:
            chat = MessagesController.getInstance(acc).getChat(-peer)
        except Exception:
            chat = None
        if chat is None:
            return ("unknown", "не в чате")
        if chat.kicked:
            return ("kicked", "исключён")
        if _is_banned(chat):
            return ("banned", "забанен")
        if chat.left or chat.deactivated:
            return ("out", "не в чате")
        return ("member", "в чате")

    def _manual_hidden_channel_ids(self):
        raw = self.get_setting("manual_hidden_channels", "") or ""
        ids = set()
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if part and part.lstrip("-").isdigit():
                ids.add(part)
        return ids

    def _hidden_channel_ids(self):
        hidden = set((self.get_setting("hidden_channels", {}) or {}).keys())
        hidden |= self._manual_hidden_channel_ids()
        return hidden

    # ------------------------------------------------------------------
    # Вспомогательное: уведомления, смена аккаунта
    # ------------------------------------------------------------------

    def _info(self, text):
        try:
            BulletinHelper.show_info(text)
        except Exception:
            log(traceback.format_exc())

    def _post_send_as_update(self, peer):
        def _p():
            try:
                NotificationCenter.getInstance(UserConfig.selectedAccount).postNotificationName(
                    NotificationCenter.updateDefaultSendAsPeer, peer, None
                )
            except Exception:
                log(traceback.format_exc())

        run_on_ui_thread(_p)

    def _switch_app_to(self, acc, reason=""):
        def _do():
            try:
                if not UserConfig.isValidAccount(acc):
                    return
                if acc == UserConfig.selectedAccount:
                    return
                la = LaunchActivity.instance
                if la is None:
                    self._info("Не удалось переключить аккаунт")
                    return
                name = self._account_name(acc)
                la.switchToAccount(acc, True)
                self.log("app switched to account {} ({})".format(acc, reason))
                run_on_ui_thread(lambda: self._info("Переключено на «{}»".format(name)), 400)
            except Exception:
                log(traceback.format_exc())

        run_on_ui_thread(_do)

    # ------------------------------------------------------------------
    # Попап «Отправить от…»: инъекция аккаунтов
    # ------------------------------------------------------------------

    def _on_popup_created(self, param):
        try:
            popup = param.thisObject
            pending = self._pending_popup_peer
            self._pending_popup_peer = None
            chat_full = get_private_field(popup, "chatFull")
            if pending is not None:
                peer = pending
            elif chat_full is not None:
                peer = -int(chat_full.id)
            else:
                return
            if peer is None or _is_encrypted(peer):
                return
            self._remember_channels(popup)
            self._inject_accounts(popup, peer)
        except Exception:
            log(traceback.format_exc())
            self._diag("popup inject error: " + traceback.format_exc().strip().splitlines()[-1])
            self._warn_once("popup", "Send as Accounts: не удалось добавить аккаунты в список (см. диагностику)")

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------

    def _show_diag(self):
        try:
            accounts = [
                a for a in range(UserConfig.MAX_ACCOUNT_COUNT)
                if UserConfig.isValidAccount(a)
            ]
            lines = [
                "Аккаунтов в приложении: {} (текущий: {})".format(
                    len(accounts), UserConfig.selectedAccount
                ),
                "Режим: {}".format(MODE_NAMES[self.get_setting("switch_mode", MODE_HYBRID)]),
                "Хуки зарегистрированы: {}/{}".format(
                    getattr(self, "_hooks_ok", 0), getattr(self, "_hooks_total", 4)
                ),
                "Запомненных отправителей: {}".format(
                    len(self._sender_map())
                ),
                "",
                "События:",
            ]
            for entry in list(getattr(self, "_diag_log", []))[-12:]:
                lines.append(entry)
            text = "\n".join(lines)

            def _run():
                try:
                    fragment = get_last_fragment()
                    activity = fragment.getParentActivity() if fragment else None
                    if activity is None:
                        self._info("Не удалось открыть диагностику")
                        return
                    builder = AlertDialogBuilder(activity)
                    builder.set_title("Send as Accounts — диагностика")
                    builder.set_message(text)

                    def _ok(bld, which):
                        try:
                            bld.dismiss()
                        except Exception:
                            pass

                    builder.set_positive_button("ОК", _ok)
                    builder.show()
                except Exception:
                    log(traceback.format_exc())

            run_on_ui_thread(_run)
        except Exception:
            log(traceback.format_exc())

    def _remember_channels(self, popup):
        """Засечь каналы из списка, чтобы их можно было скрыть в настройках."""
        try:
            send_as = get_private_field(popup, "sendAsPeers")
            if send_as is None or send_as.peers is None:
                return
            controller = MessagesController.getInstance(UserConfig.selectedAccount)
            seen = dict(self.get_setting("seen_channels", {}) or {})
            changed = False
            for p in send_as.peers:
                peer_obj = p.peer
                if peer_obj is None or peer_obj.channel_id == 0:
                    continue
                key = str(peer_obj.channel_id)
                if key in seen:
                    continue
                chat = controller.getChat(peer_obj.channel_id)
                title = "Канал {}".format(peer_obj.channel_id)
                if chat is not None and chat.title:
                    title = chat.title
                seen[key] = title
                changed = True
            if changed:
                self.set_setting("seen_channels", seen)
        except Exception:
            log(traceback.format_exc())

    def _inject_accounts(self, popup, peer):
        try:
            recycler = get_private_field(popup, "recyclerView")
            if recycler is None:
                return
            orig_adapter = recycler.getAdapter()
            orig_total = orig_adapter.getItemCount() if orig_adapter is not None else 0

            orig_positions = []
            if not self.get_setting("hide_channels", False):
                hidden_channels = self._hidden_channel_ids()
                send_as = get_private_field(popup, "sendAsPeers")
                peers_list = send_as.peers if send_as is not None else None
                for i in range(orig_total):
                    skip = False
                    if peers_list is not None and i < len(peers_list):
                        p = peers_list[i].peer
                        if (
                            p is not None
                            and p.channel_id != 0
                            and str(p.channel_id) in hidden_channels
                        ):
                            skip = True
                    if not skip:
                        orig_positions.append(i)

            accounts = self._get_accounts_for_list()
            if not orig_positions and not accounts:
                return

            state = {
                "plugin": self,
                "orig_adapter": orig_adapter,
                "orig_count": len(orig_positions),
                "orig_positions": orig_positions,
                "accounts": accounts,
                "peer": peer,
                "popup": popup,
                "orig_listener": get_private_field(recycler, "onItemClickListener"),
            }
            token = self._register_state(state)

            adapter = SenderAdapter.new_instance()
            adapter.token = token
            recycler.setAdapter(adapter.java)

            click = ClickProxy.new_instance()
            click.token = token
            recycler.setOnItemClickListener(click.java)

            long_click = LongClickProxy.new_instance()
            long_click.token = token
            recycler.setOnItemLongClickListener(long_click.java)
        except Exception:
            log(traceback.format_exc())

    def _bind_account_row(self, view, acc, peer):
        try:
            user = self._account_user(acc)
            if user is not None:
                view.avatar.setAvatar(user)
            current = UserConfig.selectedAccount
            if acc == current:
                view.title.setText(self._account_name(acc) + " — текущий")
                view.subtitle.setText("отправка от этого аккаунта")
                view.setAlpha(1.0)
            else:
                view.title.setText(self._account_name(acc))
                kind, _ = self._account_status(peer, acc)
                if self.get_setting("show_status", True):
                    view.subtitle.setText("аккаунт" if kind == "member" else kind_text(kind))
                else:
                    view.subtitle.setText("аккаунт")
                view.setAlpha(0.5 if kind in ("kicked", "banned", "out", "unknown") else 1.0)
            selected = self.get_sender_account(peer) == acc
            view.avatar.setSelected(bool(selected), False)
        except Exception:
            log(traceback.format_exc())

    def _on_original_peer_selected(self, peer):
        """Пользователь выбрал канал — сбрасываем сохранённого аккаунта."""
        self._clear_sender(peer)

    def _on_account_row_tapped(self, peer, acc, popup, view):
        try:
            if popup is not None:
                try:
                    popup.dismiss()
                except Exception:
                    pass
            if view is not None and getattr(view, "avatar", None) is not None:
                try:
                    view.avatar.setSelected(True, True)
                except Exception:
                    pass

            if acc == UserConfig.selectedAccount:
                self._clear_sender(peer)
                self._info("Отправка от текущего аккаунта")
                return

            kind, _ = self._account_status(peer, acc)
            if peer is not None and peer < 0 and kind in ("kicked", "banned", "out"):
                self._offer_join(peer, acc)
                return
            # "unknown" — локальный кэш не знает о членстве, считаем
            # аккаунт участником и отправляем оптимистично; если это
            # не так, сервер вернёт ошибку (аккаунт вне чата).
            self._apply_sender(peer, acc)
        except Exception:
            log(traceback.format_exc())

    def _show_own_popup(self, anchor_view, enter_view, dialog_id):
        """Собственный попап для чатов, где нет «отправить от канала» (ЛС, боты)."""
        try:
            context = enter_view.getContext()
            parent_fragment = get_private_field(enter_view, "parentFragment")
            controller = MessagesController.getInstance(UserConfig.selectedAccount)
            empty = TLRPC.TL_channels_sendAsPeers()

            self._pending_popup_peer = dialog_id
            popup = SenderSelectPopup(
                context,
                parent_fragment,
                controller,
                None,
                empty,
                SelectCallbackProxy.new_instance(),
            )
            popup.setOutsideTouchable(True)
            popup.setFocusable(True)
            popup.setAnimationEnabled(False)

            # Позиционирование: над кнопкой-аватаром, как в оригинале.
            loc = [0, 0]
            anchor_view.getLocationInWindow(loc)
            content = popup.getContentView()
            content.measure(
                JView.MeasureSpec.makeMeasureSpec(2400, JView.MeasureSpec.AT_MOST),
                JView.MeasureSpec.makeMeasureSpec(2400, JView.MeasureSpec.AT_MOST),
            )
            y = loc[1] - content.getMeasuredHeight() - 6
            if y < AndroidUtilities.statusBarHeight:
                y = AndroidUtilities.statusBarHeight
            popup.showAtLocation(anchor_view, Gravity.LEFT | Gravity.TOP, -6, y)
            try:
                anchor_view.setProgress(1)
            except Exception:
                pass
        except Exception:
            log(traceback.format_exc())
            self._pending_popup_peer = None

    # ------------------------------------------------------------------
    # Обработчик кнопки-аватара (вводное поле)
    # ------------------------------------------------------------------

    def _on_sender_view_created(self, param):
        try:
            enter_view = param.thisObject
            view = get_private_field(enter_view, "senderSelectView")
            if view is None or view.getTag() == VIEW_TAG_WRAPPED:
                return
            orig = view.getOnClickListener()
            if orig is None:
                return
            view.setTag(VIEW_TAG_WRAPPED)

            def handler(v):
                try:
                    dialog_id = get_private_field(enter_view, "dialog_id")
                    if _is_encrypted(dialog_id):
                        return
                    delegate = get_private_field(enter_view, "delegate")
                    if delegate is not None and delegate.getSendAsPeers() is not None:
                        # Группа с каналами — оригинальный попап (с аккаунтами).
                        orig.onClick(v)
                        return
                except Exception:
                    return
                if not self._get_accounts_for_list():
                    return
                dialog_id = get_private_field(enter_view, "dialog_id")
                self._show_own_popup(v, enter_view, dialog_id)

            view.setOnClickListener(OnClickListener(handler))
        except Exception:
            log(traceback.format_exc())

    # ------------------------------------------------------------------
    # Показ аватара выбранного аккаунта в поле ввода
    # ------------------------------------------------------------------

    def _on_update_send_as_after(self, param):
        try:
            enter_view = param.thisObject
            peer = get_private_field(enter_view, "dialog_id")
            if peer is None or _is_encrypted(peer):
                return
            acc = self.get_sender_account(peer)
            if acc is None or acc == UserConfig.selectedAccount:
                return
            user = self._account_user(acc)
            if user is None:
                return
            view = get_private_field(enter_view, "senderSelectView")
            if view is None:
                cls = find_class("org.telegram.ui.Components.ChatActivityEnterView")
                if cls is not None:
                    m = cls.getDeclaredMethod("createSenderSelectView")
                    m.setAccessible(True)
                    m.invoke(enter_view)
                    view = get_private_field(enter_view, "senderSelectView")
            if view is not None:
                view.setAvatar(user)
                view.setVisibility(JView.VISIBLE)
        except Exception:
            log(traceback.format_exc())

    # ------------------------------------------------------------------
    # Выбор отправителя
    # ------------------------------------------------------------------

    def _apply_sender(self, peer, acc):
        try:
            cur = UserConfig.selectedAccount
            if acc == cur:
                self._clear_sender(peer)
                self._info("Отправка от текущего аккаунта")
                return

            mode = self.get_setting("switch_mode", MODE_SWITCH)
            is_private = peer is not None and peer > 0 and not _is_encrypted(peer)

            if mode == MODE_FULL or (mode == MODE_HYBRID and is_private):
                self._switch_app_to(acc, reason="select")
                return

            mem = self._sender_map()
            if peer is not None:
                mem[str(peer)] = acc
            if self.get_setting("remember_sender", True):
                self.set_setting("chat_senders", mem)
            else:
                self._mem_senders = mem

            if peer is not None and peer < 0:
                self._set_local_default_send_as(peer, acc)

            self._info("Отправлять от «{}»".format(self._account_name(acc)))
        except Exception:
            log(traceback.format_exc())

    def _clear_sender(self, peer):
        try:
            if peer is None:
                return
            mem = self._sender_map()
            if str(peer) not in mem:
                return
            del mem[str(peer)]
            if self.get_setting("remember_sender", True):
                self.set_setting("chat_senders", mem)
            else:
                self._mem_senders = mem
            if peer < 0:
                full = MessagesController.getInstance(UserConfig.selectedAccount).getChatFull(-peer)
                if full is not None and full.default_send_as is not None:
                    full.default_send_as = None
                    self._post_send_as_update(peer)
        except Exception:
            log(traceback.format_exc())

    def _set_local_default_send_as(self, peer, acc):
        try:
            user = self._account_user(acc)
            if user is None:
                return
            full = MessagesController.getInstance(UserConfig.selectedAccount).getChatFull(-peer)
            if full is None:
                return
            p = TLRPC.TL_peerUser()
            p.user_id = user.id
            full.default_send_as = p
            self._post_send_as_update(peer)
        except Exception:
            log(traceback.format_exc())

    # ------------------------------------------------------------------
    # Вступление в чат от имени другого аккаунта
    # ------------------------------------------------------------------

    def _offer_join(self, peer, acc):
        try:
            cur = UserConfig.selectedAccount
            chat = None
            try:
                chat = MessagesController.getInstance(cur).getChat(-peer)
            except Exception:
                chat = None
            if chat is None:
                try:
                    chat = MessagesController.getInstance(acc).getChat(-peer)
                except Exception:
                    chat = None
            if chat is None or not ChatObject.isChannel(chat) or not chat.megagroup:
                self._info("Вступить в этот чат нельзя")
                return
            if not chat.has_link:
                self._info("Вступить нельзя: у чата нет публичной ссылки")
                return
            run_on_ui_thread(lambda: self._show_join_dialog(acc, chat.id, chat.title))
        except Exception:
            log(traceback.format_exc())

    def _show_join_dialog(self, acc, channel_id, chat_title):
        try:
            fragment = get_last_fragment()
            activity = fragment.getParentActivity() if fragment else None
            if activity is None:
                self._info("Не удалось открыть диалог")
                return
            acc_name = self._account_name(acc)
            title = chat_title or "чат {}".format(channel_id)
            builder = AlertDialogBuilder(activity)
            builder.set_title("Вступить в чат?")
            builder.set_message(
                "Аккаунт «{}» вступит в «{}».".format(acc_name, title)
            )

            def on_ok(bld, which):
                try:
                    bld.dismiss()
                except Exception:
                    pass
                self._do_join(acc, channel_id)

            def on_cancel(bld, which):
                try:
                    bld.dismiss()
                except Exception:
                    pass

            builder.set_positive_button("Вступить", on_ok)
            builder.set_negative_button("Отмена", on_cancel)
            builder.show()
        except Exception:
            log(traceback.format_exc())

    def _do_join(self, acc, channel_id):
        try:
            cb_cls = _make_join_callback_class()
            if cb_cls is None:
                self._info("Не удалось выполнить вступление")
                return
            token = self._register_state({"on_result": self._make_join_result_cb(acc)})
            cb = cb_cls.new_instance()
            cb.token = token
            mc = MessagesController.getInstance(acc)
            req = TLRPC.TL_channels_joinChannel()
            req.channel = mc.getInputChannel(channel_id)
            ConnectionsManager.getInstance(acc).sendRequest(req, cb)
        except Exception:
            log(traceback.format_exc())
            self._info("Не удалось вступить в чат")

    def _make_join_result_cb(self, acc):
        def cb(response, error):
            try:
                if error is not None:
                    msg = getattr(error, "text", None) or "Не удалось вступить в чат"
                    run_on_ui_thread(lambda: self._info(msg))
                    return
                if response is not None:
                    try:
                        MessagesController.getInstance(acc).processUpdates(response, True)
                    except Exception:
                        pass
                    name = self._account_name(acc)
                    run_on_ui_thread(
                        lambda: BulletinHelper.show_success(
                            "«{}» вступил в чат".format(name)
                        )
                    )
            except Exception:
                log(traceback.format_exc())
        return cb

    # ------------------------------------------------------------------
    # Перенаправление отправки (группы / ЛС / боты)
    # ------------------------------------------------------------------

    def _validate_target(self, peer, cur, target):
        if peer is None or peer > 0:
            return target
        try:
            chat = MessagesController.getInstance(target).getChat(-peer)
        except Exception:
            return target
        if chat is None:
            return target
        if chat.kicked:
            name = self._account_name(target)
            run_on_ui_thread(
                lambda: BulletinHelper.show_error(
                    "«{}» исключён из этого чата".format(name)
                )
            )
            return None
        if _is_banned(chat):
            name = self._account_name(target)
            run_on_ui_thread(
                lambda: BulletinHelper.show_error(
                    "«{}» заблокирован в этом чате".format(name)
                )
            )
            return None
        return target

    def _box_args(self, args, n):
        boxed = list(args)
        if n == 27:
            prim = {
                10: JLong, 15: JBoolean, 20: JBoolean, 21: JInteger,
                22: JInteger, 25: JBoolean, 26: JBoolean,
            }
        else:
            prim = {1: JLong, 2: JBoolean, 3: JBoolean, 4: JBoolean, 5: JInteger}
        for idx, cls in prim.items():
            v = boxed[idx]
            if type(v) in (int, bool):
                boxed[idx] = cls(v)
        return boxed

    def _on_send_before(self, param):
        redirected = False
        try:
            if getattr(self._tls, "guard", 0) > 0:
                return
            args = param.args
            if args is None:
                return
            n = len(args)
            if n == 27:
                peer = args[10]
                retry = args[16]
            elif n == 7:
                peer = args[1]
                retry = None
            else:
                return
            if retry is not None:
                return
            try:
                peer = int(peer)
            except Exception:
                return
            if _is_encrypted(peer):
                return
            helper = param.thisObject
            account = get_private_field(helper, "currentAccount")
            if account is None:
                account = UserConfig.selectedAccount
            target = self.get_sender_account(peer)
            if target is None or target == account:
                return
            # Аккаунт мог быть вылогинен после выбора отправителя.
            try:
                if not UserConfig.getInstance(target).isClientActivated():
                    run_on_ui_thread(lambda: self._info("Аккаунт больше не доступен"))
                    self._clear_sender(peer)
                    return
            except Exception:
                pass
            target = self._validate_target(peer, account, target)
            if target is None or target == account:
                return

            self._tls.guard = 1
            param.setResult(None)
            self._last_redirect = (peer, target)
            redirected = True
            boxed = self._box_args(args, n)
            target_helper = AccountInstance.getInstance(target).getSendMessagesHelper()
            param.method.invoke(target_helper, jarray("Ljava/lang/Object;", boxed))
        except Exception:
            self._last_redirect = None
            log(traceback.format_exc())
            if redirected:
                try:
                    run_on_ui_thread(
                        lambda: BulletinHelper.show_error(
                            "Не удалось отправить от другого аккаунта"
                        )
                    )
                except Exception:
                    pass
        finally:
            self._tls.guard = 0

    def _on_send_after(self, param):
        rd = getattr(self, "_last_redirect", None)
        if rd is None:
            return
        peer, target = rd
        self._last_redirect = None
        try:
            if not self.get_setting("show_sent_as", True):
                return
            name = self._account_name(target)
            if peer is not None and peer > 0:
                # ЛС / бот (режим переключения отправителя): предлагаем
                # переключить приложение на отправителя.
                def on_switch():
                    self._switch_app_to(target, reason="bulletin")

                BulletinHelper.show_with_button(
                    "Отправлено от «{}»".format(name),
                    R_tg.raw.info,
                    "Переключиться",
                    on_switch,
                )
            else:
                BulletinHelper.show_info("Отправлено от «{}»".format(name))
        except Exception:
            log(traceback.format_exc())

    # ------------------------------------------------------------------
    # Пункт меню чата «Отправлять от…»
    # ------------------------------------------------------------------

    def _on_chat_action_menu(self, context):
        try:
            dialog_id = None
            if isinstance(context, dict):
                dialog_id = context.get("dialog_id")
                if dialog_id is None:
                    fragment = context.get("fragment")
                    if fragment is not None:
                        dialog_id = get_private_field(fragment, "dialog_id")
            if dialog_id is None:
                self.log(
                    "Send as Accounts: no dialog_id in menu context: {}".format(
                        list(context.keys()) if isinstance(context, dict) else context
                    )
                )
                return
            dialog_id = int(dialog_id)

            if _is_encrypted(dialog_id):
                self._info("Недоступно для секретных чатов")
                return
            if dialog_id < 0:
                chat = None
                try:
                    chat = MessagesController.getInstance(
                        UserConfig.selectedAccount
                    ).getChat(-dialog_id)
                except Exception:
                    chat = None
                if chat is not None and ChatObject.isChannel(chat) and not chat.megagroup:
                    self._info("Недоступно для каналов")
                    return
            self._show_accounts_dialog(dialog_id)
        except Exception:
            log(traceback.format_exc())

    def _show_accounts_dialog(self, dialog_id):
        accounts = self._get_accounts_for_list()
        if not accounts:
            self._info("Нет доступных аккаунтов")
            return
        is_private = dialog_id > 0
        items = []
        for acc in accounts:
            name = self._account_name(acc)
            if acc == UserConfig.selectedAccount:
                items.append(name + " (текущий)")
            elif is_private:
                items.append(name)
            else:
                kind, text = self._account_status(dialog_id, acc)
                items.append(name + " — " + text if kind != "member" else name)

        def on_item(bld, index):
            try:
                bld.dismiss()
            except Exception:
                pass
            acc = accounts[index]
            if acc == UserConfig.selectedAccount:
                self._clear_sender(dialog_id)
                self._info("Отправка от текущего аккаунта")
                return
            kind, _ = self._account_status(dialog_id, acc)
            if not is_private and kind in ("kicked", "banned", "out"):
                self._offer_join(dialog_id, acc)
                return
            self._apply_sender(dialog_id, acc)

        def _run():
            try:
                fragment = get_last_fragment()
                activity = fragment.getParentActivity() if fragment else None
                if activity is None:
                    self._info("Не удалось открыть список аккаунтов")
                    return
                builder = AlertDialogBuilder(activity)
                builder.set_title("Отправлять от…")
                builder.set_items(items, on_item)
                builder.show()
            except Exception:
                log(traceback.format_exc())

        run_on_ui_thread(_run)

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------

    def _reset_hidden_accounts(self):
        try:
            self.set_setting("hidden_accounts", {}, reload_settings=True)
        except Exception:
            log(traceback.format_exc())

    def _reset_hidden_channels(self):
        try:
            self.set_setting("hidden_channels", {}, reload_settings=True)
        except Exception:
            log(traceback.format_exc())

    def _reset_order(self):
        try:
            self.set_setting("account_order", [], reload_settings=True)
        except Exception:
            log(traceback.format_exc())

    def _move_account(self, acc, delta):
        try:
            order = self._get_order()
            i = order.index(acc)
            j = i + delta
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                self.set_setting("account_order", order, reload_settings=True)
        except Exception:
            log(traceback.format_exc())

    def _accounts_hidden_subpage(self):
        rows = [
            Header(text="Скрытые аккаунты"),
            Text(
                text="Скрытые аккаунты не показываются в списке отправителей.",
                subtext="Снимите отметку, чтобы показать аккаунт снова.",
                icon="msg_user_search",
            ),
        ]
        for acc in range(UserConfig.MAX_ACCOUNT_COUNT):
            if not UserConfig.isValidAccount(acc):
                continue
            subtext = (
                "Текущий аккаунт"
                if acc == UserConfig.selectedAccount
                else "Аккаунт {}".format(acc + 1)
            )
            rows.append(
                Switch(
                    key="hidden_account_{}".format(acc),
                    text=self._account_name(acc),
                    subtext=subtext,
                    default=False,
                )
            )
        rows.append(
            Text(
                text="Сбросить скрытые аккаунты",
                icon="msg_arrow_forward",
                on_click=lambda v: self._reset_hidden_accounts(),
            )
        )
        return rows

    def _channels_hidden_subpage(self):
        rows = [
            Header(text="Скрытые каналы"),
            Text(
                text="Каналы появляются в списке, когда вы открываете меню «Отправить от…» в чате с привязанным каналом.",
                icon="msg_list",
            ),
        ]
        seen = self.get_setting("seen_channels", {}) or {}
        for ch_id in sorted(seen.keys(), key=lambda x: -int(x)):
            rows.append(
                Switch(
                    key="hidden_channel_{}".format(ch_id),
                    text=seen[ch_id],
                    subtext="ID: {}".format(ch_id),
                    default=False,
                )
            )
        rows.append(
            Input(
                key="manual_hidden_channels",
                text="Скрыть каналы по ID",
                subtext="ID через запятую, например: 1234567890, 987654321",
                default="",
                icon="msg_list",
            )
        )
        rows.append(
            Text(
                text="Сбросить скрытые каналы",
                icon="msg_arrow_forward",
                on_click=lambda v: self._reset_hidden_channels(),
            )
        )
        return rows

    def _order_subpage(self):
        order = self._get_order()
        rows = [
            Header(text="Порядок аккаунтов"),
            Text(
                text="Нажатие — поднять выше; долгое нажатие — опустить ниже.",
                subtext="Порядок применяется в списке отправителей.",
                icon="msg_list",
            ),
        ]
        for i, acc in enumerate(order):
            rows.append(
                Text(
                    text="{}. {}".format(i + 1, self._account_name(acc)),
                    subtext="нажатие: выше · зажать: ниже",
                    icon="msg_arrow_forward",
                    on_click=lambda v, a=acc: self._move_account(a, -1),
                    on_long_click=lambda v, a=acc: self._move_account(a, +1),
                )
            )
        rows.append(
            Text(
                text="Сбросить порядок",
                icon="msg_arrow_forward",
                on_click=lambda v: self._reset_order(),
            )
        )
        return rows

    def create_settings(self) -> List[Any]:
        return [
            Header(text="Send as Accounts"),
            Text(
                text="Переписка от других аккаунтов",
                subtext=(
                    "В группах сообщения уходят от выбранного аккаунта, "
                    "в ЛС и ботах — отправка от аккаунта с предложением "
                    "переключить приложение. Долгое нажатие на аккаунт — "
                    "полная смена аккаунта."
                ),
                icon="msg_user_search",
            ),
            Divider(),
            Header(text="Режим"),
            Selector(
                key="switch_mode",
                text="Режим смены",
                default=MODE_HYBRID,
                items=list(MODE_NAMES),
                icon="msg_settings",
            ),
            Switch(
                key="long_press_switch",
                text="Долгое нажатие — полная смена",
                subtext="Полностью переключает приложение на аккаунт, независимо от режима",
                default=True,
                icon="msg_settings",
            ),
            Divider(),
            Header(text="Список отправителей"),
            Switch(
                key="hide_channels",
                text="Скрыть каналы в списке",
                subtext="В меню «Отправить от…» показывать только личные аккаунты",
                default=False,
                icon="msg_list",
            ),
            Text(
                text="Скрытые аккаунты",
                subtext="Какие аккаунты скрывать из списка",
                icon="msg_arrow_forward",
                create_sub_fragment=self._accounts_hidden_subpage,
            ),
            Text(
                text="Скрытые каналы",
                subtext="Каналы из меню «Отправить от…»",
                icon="msg_arrow_forward",
                create_sub_fragment=self._channels_hidden_subpage,
            ),
            Text(
                text="Порядок аккаунтов",
                subtext="Изменить порядок в списке",
                icon="msg_arrow_forward",
                create_sub_fragment=self._order_subpage,
            ),
            Divider(),
            Header(text="Уведомления и статусы"),
            Switch(
                key="show_sent_as",
                text="Уведомление «отправлено от»",
                subtext="Сплывающее уведомление после отправки; в ЛС с кнопкой «Переключиться»",
                default=True,
                icon="msg_settings",
            ),
            Switch(
                key="remember_sender",
                text="Запоминать отправителя для чата",
                subtext="Сохранять выбор аккаунта для каждого чата",
                default=True,
                icon="msg_settings",
            ),
            Switch(
                key="show_status",
                text="Показывать статусы",
                subtext="«Не в чате», «Исключён», «Забанен» в списке аккаунтов",
                default=True,
                icon="msg_settings",
            ),
        ]
