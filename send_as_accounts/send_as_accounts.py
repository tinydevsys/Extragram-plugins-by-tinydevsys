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

import sys
import threading
import time
import traceback
from typing import Any, List

from base_plugin import (
    BasePlugin,
    HookResult,
    HookStrategy,
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

from java import jarray
from java.lang import Boolean as JBoolean
from java.lang import Class as JClass
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
__version__ = "1.0.5"
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
    """Настоящий объект java.lang.Class для рефлексии.

    ВАЖНО: в Chaquopy и find_class(), и jclass() возвращают Python-тип
    (обёртку), у которого НЕТ методов рефлексии (getDeclaredMethod и
    т.п.). Настоящий java.lang.Class даёт только статический вызов
    java.lang.Class.forName(...).
    """
    try:
        cls = JClass.forName(name)
        if cls is not None and hasattr(cls, "getDeclaredMethod"):
            return cls
    except Exception:
        pass
    # Резерв: вытащить Class-экземпляр из Python-типа (внутренности Chaquopy).
    try:
        pytype = find_class(name)
        j_klass = getattr(pytype, "_chaquopy_j_klass", None)
        if j_klass is not None:
            return JClass(instance=j_klass)
    except Exception:
        pass
    return None


def _type_desc(obj):
    try:
        return "{}(reflect={})".format(type(obj).__name__, hasattr(obj, "getDeclaredMethod"))
    except Exception:
        return repr(obj)[:60]


def _exc_info(exc):
    """Читаемое описание исключения (для диагностики)."""
    try:
        msg = str(exc) or type(exc).__name__
        return "{}: {}".format(type(exc).__name__, msg)[:250]
    except Exception:
        try:
            return type(exc).__name__
        except Exception:
            return "error"


def _field_by_type(obj, type_name):
    """Поиск поля объекта по типу Java-класса (запасной путь, если имя
    поля в этой версии клиента другое)."""
    try:
        short = type_name.rsplit(".", 1)[-1]
        cls = obj.getClass()
        while cls is not None:
            for f in cls.getDeclaredFields():
                try:
                    fname = f.getName()
                    ftype = f.getType().getName()
                    if ftype == type_name or ftype.rsplit(".", 1)[-1] == short:
                        f.setAccessible(True)
                        v = f.get(obj)
                        if v is not None:
                            return v
                except Exception:
                    continue
            cls = cls.getSuperclass()
    except Exception:
        pass
    return None


def _find_sender_view(enter_view):
    """Кнопка-аватар: поле senderSelectView или поиск по классу
    SenderSelectView среди потомков (имя поля может отличаться)."""
    view = get_private_field(enter_view, "senderSelectView")
    if view is not None:
        return view
    try:
        cls = JClass.forName("org.telegram.ui.Components.SenderSelectView")
        from android.view import ViewGroup as JViewGroup

        stack = [enter_view]
        seen = 0
        while stack and seen < 200:
            parent = stack.pop()
            seen += 1
            try:
                n = parent.getChildCount()
            except Exception:
                continue
            for i in range(n):
                try:
                    child = parent.getChildAt(i)
                except Exception:
                    continue
                if child is None:
                    continue
                if cls.isInstance(child):
                    return child
                if isinstance(child, JViewGroup):
                    stack.append(child)
    except Exception:
        pass
    return None


def _enter_view_dialog_id(enter_view):
    """id диалога входного поля: поле dialog_id или текущий фрагмент."""
    try:
        d = get_private_field(enter_view, "dialog_id")
        if d is not None:
            return int(d)
    except Exception:
        pass
    try:
        frag = get_last_fragment()
        if frag is not None and "ChatActivity" in frag.getClass().getName():
            return int(frag.getDialogId())
    except Exception:
        pass
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

    @classmethod
    def _make_sender_view(cls, context, resources_provider):
        """Строка списка: SenderView(context, resourcesProvider) в новых
        версиях, SenderView(context) в старых."""
        if resources_provider is not None:
            order = [(context, resources_provider), (context,)]
        else:
            order = [(context,), (context, None)]
        for args in order:
            try:
                return SenderSelectPopup.SenderView(*args)
            except Exception:
                continue
        return None

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
        # Сначала личные аккаунты (viewType 1), потом каналы (viewType 0).
        return 1 if position < len(st["accounts"]) else 0

    @joverride()
    def onCreateViewHolder(self, parent, viewType):
        st = self._st()
        if st is None:
            return None
        if viewType == 0 and st["orig_count"] > 0 and st["orig_adapter"] is not None:
            return st["orig_adapter"].onCreateViewHolder(parent, 0)
        context = parent.getContext() if parent is not None else None
        view = self._make_sender_view(context, st.get("resources_provider"))
        if view is None:
            return None
        return RecyclerListView.Holder(view)

    @joverride()
    def onBindViewHolder(self, holder, position):
        st = self._st()
        if st is None:
            return
        n_acc = len(st["accounts"])
        if position < n_acc:
            st["plugin"]._bind_account_row(holder.itemView, st["accounts"][position], st["peer"])
        else:
            st["orig_adapter"].onBindViewHolder(holder, st["orig_positions"][position - n_acc])


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
        n_acc = len(st["accounts"])
        try:
            if position < n_acc:
                acc = st["accounts"][position]
                plugin._on_account_row_tapped(st["peer"], acc, st["popup"], view)
            else:
                # Сначала сбрасываем сохранённого аккаунта, чтобы оригинальный
                # поток (updateSendAsButton) уже не подменял аватар обратно.
                plugin._on_original_peer_selected(st["peer"])
                orig_pos = st["orig_positions"][position - n_acc]
                if st.get("enter_view") is not None:
                    # Собственный попап: повторяем действия оригинала.
                    plugin._on_own_channel_selected(st, orig_pos)
                elif st["orig_listener"] is not None:
                    st["orig_listener"].onItemClick(view, orig_pos)
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
            n_acc = len(st["accounts"])
            if position < n_acc and plugin.get_setting("long_press_switch", True):
                acc = st["accounts"][position]
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
        except Exception as e:
            log(traceback.format_exc())
            self._diag("menu item: " + _exc_info(e))
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

    def _probe(self, name, interval=60):
        probe_last = getattr(self, "_probe_last", None)
        if probe_last is None:
            probe_last = self._probe_last = {}
        if time.time() - probe_last.get(name, 0) < interval:
            return
        probe_last[name] = time.time()
        self._diag("{} fired".format(name))

    def _diag_once(self, name, text):
        """Строка в диагностику один раз за сессию (не спамить)."""
        done = getattr(self, "_diag_once_set", None)
        if done is None:
            done = self._diag_once_set = set()
        if name in done:
            return
        done.add(name)
        self._diag(text)

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
            self._diag("popup: cls={}".format(_type_desc(cls)))
            # Сигнатура конструктора различается между версиями клиента,
            # поэтому ищем его по содержимому: нужен тот, где есть
            # OnSelectCallback.
            best = None
            sigs = []
            try:
                for c in cls.getDeclaredConstructors():
                    names = [t.getName() for t in c.getParameterTypes()]
                    short = [n.rsplit(".", 1)[-1] for n in names]
                    sigs.append("(" + ",".join(short) + ")")
                    if any("OnSelectCallback" in n for n in names):
                        if best is None or len(names) > len(
                            best.getParameterTypes()
                        ):
                            best = c
            except Exception:
                pass
            self._diag("popup ctors: " + (" | ".join(sigs) if sigs else "не получены"))
            if best is None:
                self._diag("popup hook: ctor with OnSelectCallback not found")
                return
            self._popup_ctor_types = [t.getName() for t in best.getParameterTypes()]
            try:
                best.setAccessible(True)
                self.hook_method(best, PopupCtorHook(self))
                self._hooks_ok += 1
                self._diag("popup hook: ok (ctor)")
            except Exception as e_ctor:
                # Мост не хукает отдельный конструктор — хукаем все.
                self._diag(
                    "popup ctor failed: {} -> hook_all_constructors".format(
                        _exc_info(e_ctor)
                    )
                )
                self.hook_all_constructors(cls, PopupCtorHook(self))
                self._hooks_ok += 1
                self._diag("popup hook: ok (all ctors)")
        except Exception as e:
            log(traceback.format_exc())
            self._diag("popup hook: " + _exc_info(e))

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
        except Exception as e:
            log(traceback.format_exc())
            self._diag("sender_view hook: " + _exc_info(e))

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
        except Exception as e:
            log(traceback.format_exc())
            self._diag("update_send_as hook: " + _exc_info(e))

    def _hook_send(self):
        try:
            cls = _jclass("org.telegram.messenger.SendMessagesHelper")
            if cls is None:
                self.log("Send as Accounts: SendMessagesHelper class not found")
                self._diag("send hook: SendMessagesHelper class not found")
                return
            methods = [
                m for m in cls.getDeclaredMethods() if m.getName() == "sendMessage"
            ]
            self._diag(
                "send: overloads="
                + ",".join(str(len(m.getParameterTypes())) for m in methods)
            )
            # Новая архитектура клиента: весь трафик отправки идёт через
            # sendMessage(SendMessageParams), в начале которого вызывается
            # официальный хук плагина executeSendMessageHook — его и
            # используем (см. on_send_message_hook ниже).
            has_params_api = any(
                "SendMessageParams" in m.getParameterTypes()[0].getName()
                for m in methods
                if len(m.getParameterTypes()) == 1
            )
            if has_params_api:
                self.add_on_send_message_hook()
                self._hooks_ok += 1
                self._diag("send hook: ok (SendMessageParams api)")
            hooked = 0
            for m in methods:
                pt = m.getParameterTypes()
                n = len(pt)
                if n == 27:
                    # Старый API: основной путь отправки.
                    m.setAccessible(True)
                    self.hook_method(m, SendRedirectHook(self))
                    hooked += 1
                elif n == 7 and pt[1].getName() == "long":
                    # Старый API: пересылка (…, int, MessageObject).
                    # Новый API (…, int, long) не хукаем — его закрывает
                    # официальный хук.
                    if has_params_api or pt[6].getName() == "long":
                        continue
                    m.setAccessible(True)
                    self.hook_method(m, SendRedirectHook(self))
                    hooked += 1
            if not has_params_api:
                if hooked:
                    self._hooks_ok += 1
                    self._diag("send hook: ok ({} legacy methods)".format(hooked))
                else:
                    self._diag("send hook: no matching sendMessage overloads")
        except Exception as e:
            log(traceback.format_exc())
            self._diag("send hook: " + _exc_info(e))

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
            peer = None
            if pending is not None:
                peer = pending
            else:
                # Старые версии: поле chatFull; новые — ChatActivity
                # в аргументах конструктора.
                chat_full = get_private_field(popup, "chatFull")
                if chat_full is not None:
                    peer = -int(chat_full.id)
                else:
                    peer = self._peer_from_ctor_args(param)
            if peer is None:
                self._diag("popup created: chat id не определён")
                return
            if _is_encrypted(peer):
                self._diag("popup created: секретный чат — пропуск")
                return
            self._diag("popup created: peer={}".format(peer))
            self._remember_channels(popup)
            own_enter_view = getattr(self, "_pending_own_enter_view", None)
            self._pending_own_enter_view = None
            self._inject_accounts(
                popup,
                peer,
                self._resources_provider_from_args(self._ctor_args(param)),
                own_enter_view,
            )
        except Exception as e:
            log(traceback.format_exc())
            self._diag("popup inject error: " + _exc_info(e))
            self._warn_once("popup", "Send as Accounts: не удалось добавить аккаунты в список (см. диагностику)")

    def _ctor_args(self, param):
        args = getattr(param, "args", None)
        return list(args) if args else []

    def _peer_from_ctor_args(self, param):
        for a in self._ctor_args(param):
            if a is None:
                continue
            try:
                if a.getClass().getName() == "org.telegram.ui.ChatActivity":
                    return int(a.getDialogId())
            except Exception:
                continue
        # Запасной путь: текущий фрагмент (попап открывается поверх чата).
        try:
            frag = get_last_fragment()
            if (
                frag is not None
                and frag.getClass().getName() == "org.telegram.ui.ChatActivity"
            ):
                return int(frag.getDialogId())
        except Exception:
            pass
        return None

    def _resources_provider_from_args(self, objs):
        for o in objs or []:
            if o is None:
                continue
            try:
                if "ResourcesProvider" in o.getClass().getName():
                    return o
            except Exception:
                continue
        return None

    def _build_popup_args(self, context, parent_fragment, controller, send_as, callback, resources_provider):
        """Аргументы конструктора SenderSelectPopup под сигнатуру,
        обнаруженную в _hook_popup (между версиями она различается)."""
        names = getattr(self, "_popup_ctor_types", None)
        if not names:
            # Старая 6-арг. сигнатура.
            return [context, parent_fragment, controller, None, send_as, callback]
        args = []
        for n in names:
            if n == "android.content.Context":
                args.append(context)
            elif n == "org.telegram.ui.ChatActivity":
                args.append(parent_fragment)
            elif n == "org.telegram.messenger.MessagesController":
                args.append(controller)
            elif n == "boolean":
                args.append(False)
            elif n.endswith("$Peer"):
                args.append(None)
            elif "sendAsPeers" in n:
                args.append(send_as)
            elif "OnSelectCallback" in n:
                args.append(callback)
            elif "ResourcesProvider" in n:
                args.append(resources_provider)
            else:
                args.append(None)
        return args

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
                "Версия: {}".format(__version__),
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

    def _inject_accounts(self, popup, peer, resources_provider=None, enter_view=None):
        try:
            recycler = get_private_field(popup, "recyclerView")
            if recycler is None:
                recycler = _field_by_type(popup, "androidx.recyclerview.widget.RecyclerView")
                if recycler is None:
                    self._diag("inject: recyclerView не найден")
                    return
            orig_adapter = recycler.getAdapter()
            orig_total = orig_adapter.getItemCount() if orig_adapter is not None else 0
            self._diag("inject: popup peer={} orig_rows={}".format(peer, orig_total))

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
                "resources_provider": resources_provider,
                "enter_view": enter_view,
                "send_as": get_private_field(popup, "sendAsPeers"),
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
            self._diag(
                "accounts injected: {} acc + {} ch (peer {})".format(
                    len(accounts), len(orig_positions), peer
                )
            )
        except Exception as e:
            log(traceback.format_exc())
            self._diag("inject error: " + _exc_info(e))

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

    def _on_own_channel_selected(self, st, orig_pos):
        """Выбор канала в собственном попапе: повторяем действия
        оригинального обработчика (память «отправить от» + кнопка)."""
        try:
            send_as = st.get("send_as")
            peer_obj = None
            if send_as is not None:
                peers = getattr(send_as, "peers", None) or []
                if orig_pos < len(peers):
                    peer_obj = peers[orig_pos].peer
            if peer_obj is not None:
                peer_id = 0
                if peer_obj.channel_id != 0:
                    peer_id = -peer_obj.channel_id
                elif peer_obj.user_id != 0:
                    peer_id = peer_obj.user_id
                if peer_id != 0:
                    try:
                        MessagesController.getInstance(
                            UserConfig.selectedAccount
                        ).setDefaultSendAs(int(st["peer"]), peer_id)
                    except Exception:
                        pass
            enter_view = st.get("enter_view")
            if enter_view is not None:
                try:
                    enter_view.updateSendAsButton()
                except Exception:
                    pass
            if st.get("popup") is not None:
                try:
                    st["popup"].dismiss()
                except Exception:
                    pass
        except Exception:
            log(traceback.format_exc())

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

    def _show_own_popup(self, anchor_view, enter_view, dialog_id, send_as=None):
        """Собственный попап: аккаунты + каналы (если в чате есть
        «отправить от канала»). В отличие от нативного, не зависит от
        хука на конструктор SenderSelectPopup — конструктор вызываем
        сами. Возвращает True, если окно открылось."""
        try:
            context = enter_view.getContext()
            parent_fragment = get_private_field(enter_view, "parentFragment")
            controller = MessagesController.getInstance(UserConfig.selectedAccount)
            if send_as is None:
                send_as = TLRPC.TL_channels_sendAsPeers()
            n_ch = 0
            try:
                if getattr(send_as, "peers", None):
                    n_ch = len(send_as.peers)
            except Exception:
                pass
            resources_provider = get_private_field(enter_view, "resourcesProvider")
            self._pending_popup_peer = dialog_id
            self._pending_own_enter_view = enter_view
            try:
                popup = SenderSelectPopup(
                    *self._build_popup_args(
                        context,
                        parent_fragment,
                        controller,
                        send_as,
                        SelectCallbackProxy.new_instance(),
                        resources_provider,
                    )
                )
            except Exception as e:
                self._pending_popup_peer = None
                self._pending_own_enter_view = None
                log(traceback.format_exc())
                self._diag("own popup ctor failed: " + _exc_info(e))
                return False
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
            self._diag("own popup opened: dialog={} channels={}".format(dialog_id, n_ch))
            return True
        except Exception as e:
            self._pending_popup_peer = None
            self._pending_own_enter_view = None
            log(traceback.format_exc())
            self._diag("own popup error: " + _exc_info(e))
            return False

    # ------------------------------------------------------------------
    # Обработчик кнопки-аватара (вводное поле)
    # ------------------------------------------------------------------

    def _on_sender_view_created(self, param):
        try:
            self._probe("createSenderSelectView")
            self._ensure_sender_wrapped(param.thisObject)
        except Exception:
            log(traceback.format_exc())

    def _ensure_sender_wrapped(self, enter_view):
        """Ставим свой обработчик клика по кнопке-аватару (идемпоотно)."""
        try:
            view = _find_sender_view(enter_view)
            if view is None:
                self._diag_once("wrap_no_view", "sender view: кнопка не найдена")
                return
            if view.getTag() == VIEW_TAG_WRAPPED:
                return
            orig = view.getOnClickListener()
            if orig is None:
                self._diag_once("wrap_no_listener", "sender view: нет обработчика клика")
                return
            plugin = self

            def handler(v):
                try:
                    dialog_id = _enter_view_dialog_id(enter_view)
                    if dialog_id is None or _is_encrypted(dialog_id):
                        return
                    send_as = None
                    try:
                        delegate = get_private_field(enter_view, "delegate")
                        if delegate is not None:
                            send_as = delegate.getSendAsPeers()
                    except Exception:
                        send_as = None
                    plugin._diag(
                        "avatar clicked: dialog={} channels={}".format(
                            dialog_id, "yes" if send_as is not None else "no"
                        )
                    )
                    if not plugin._get_accounts_for_list():
                        # Других аккаунтов нет — оригинальное поведение.
                        try:
                            orig.onClick(v)
                        except Exception:
                            pass
                        return
                    # Всегда открываем собственный попап (аккаунты + каналы):
                    # он не зависит от хука на конструктор SenderSelectPopup.
                    if not plugin._show_own_popup(v, enter_view, dialog_id, send_as):
                        if send_as is not None:
                            try:
                                orig.onClick(v)
                            except Exception:
                                pass
                except Exception:
                    log(traceback.format_exc())

            view.setOnClickListener(OnClickListener(handler))
            view.setTag(VIEW_TAG_WRAPPED)
            self._diag("sender view wrapped")
        except Exception as e:
            log(traceback.format_exc())
            self._diag_once("wrap_error", "sender view wrap error: " + _exc_info(e))

    # ------------------------------------------------------------------
    # Показ аватара выбранного аккаунта в поле ввода
    # ------------------------------------------------------------------

    def _on_update_send_as_after(self, param):
        try:
            self._probe("updateSendAsButton")
            enter_view = param.thisObject
            self._ensure_sender_wrapped(enter_view)
            peer = _enter_view_dialog_id(enter_view)
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
                # Отменяем возможную анимацию скрытия (в ЛС оригинальный код
                # прячет кнопку, т.к. ChatFull нет) — иначе она сработает
                # через 150 мс и уберёт нашу кнопку.
                try:
                    anim = view.getTag()
                    if anim is not None and hasattr(anim, "cancel"):
                        anim.cancel()
                        view.setTag(None)
                except Exception:
                    pass
                try:
                    view.setAvatar(user)
                except Exception:
                    pass
                view.setVisibility(JView.VISIBLE)
                try:
                    view.setAlpha(1.0)
                    view.setTranslationX(0)
                except Exception:
                    pass
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
            elif peer is not None and peer > 0:
                # ЛС/бот: ChatFull нет, кнопку показывает хук на
                # updateSendAsButton после уведомления updateDefaultSendAsPeer.
                self._post_send_as_update(peer)

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
            elif peer > 0:
                # ЛС/бот: попросим оригинальный код скрыть кнопку.
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

    def _show_sent_as_bulletin(self, peer, target):
        """Плавающее уведомление «отправлено от …» (+кнопка в ЛС)."""
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

    def _should_redirect(self, account, peer):
        """Проверенный аккаунт-отправитель для peer, или None."""
        target = self.get_sender_account(peer)
        if target is None or target == account:
            return None
        try:
            if not UserConfig.getInstance(target).isClientActivated():
                run_on_ui_thread(lambda: self._info("Аккаунт больше не доступен"))
                self._clear_sender(peer)
                return None
        except Exception:
            pass
        return self._validate_target(peer, account, target)

    def on_send_message_hook(self, account, params):
        """Официальный хук исходящих сообщений.

        Срабатывает в начале SendMessagesHelper.sendMessage(params)
        для каждого аккаунта. Если для чата выбран другой отправитель —
        выполняем отправку через его SendMessagesHelper и отменяем
        оригинальную (strategy=CANCEL).
        """
        try:
            peer = getattr(params, "peer", None)
            if peer is None:
                return HookResult()
            peer = int(peer)
            if getattr(params, "retryMessageObject", None) is not None:
                return HookResult()
            if _is_encrypted(peer):
                return HookResult()
            target = self._should_redirect(account, peer)
            if target is None:
                return HookResult()
            self._last_redirect = (peer, target)
            target_helper = AccountInstance.getInstance(target).getSendMessagesHelper()
            target_helper.sendMessage(params)
            self._diag("send redirect -> account {} (peer {})".format(target, peer))
            self._show_sent_as_bulletin(peer, target)
            return HookResult(strategy=HookStrategy.CANCEL)
        except Exception:
            self._last_redirect = None
            log(traceback.format_exc())
            self._diag("send redirect error: " + _exc_info(sys.exc_info()[1]))
            self._warn_once(
                "send",
                "Не удалось отправить от другого аккаунта — отправлено от текущего",
            )
            return HookResult()

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
        self._show_sent_as_bulletin(peer, target)

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
