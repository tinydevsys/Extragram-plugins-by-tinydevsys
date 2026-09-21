# -*- coding: utf-8 -*-
"""Стеки (stub-модули) SDK exteraGram и Java-классов для desktop-тестов.

Позволяют импортировать плагин и проверять его логику вне приложения.
"""

import sys
import types
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# java / android базовые
# ---------------------------------------------------------------------------

def _install(name, module):
    sys.modules[name] = module
    return module


def _mod(name):
    m = types.ModuleType(name)
    _install(name, m)
    return m


java = _mod("java")
java.jarray = lambda sig, values: list(values)


def _jclass_stub(name):
    # Лениво: default_find_class определяется ниже.
    return default_find_class(name)


java.jclass = _jclass_stub

java_lang = _mod("java.lang")
java.lang = java_lang


class _JavaNumeric:
    def __init__(self, value):
        self.value = value

    def __int__(self):
        return int(self.value)

    def __eq__(self, other):
        return isinstance(other, _JavaNumeric) and int(self) == int(other)

    def __repr__(self):
        return "{}({})".format(type(self).__name__, self.value)


class Long(_JavaNumeric):
    pass


class Integer(_JavaNumeric):
    pass


class Boolean(_JavaNumeric):
    def __init__(self, value):
        self.value = bool(value)

    def __eq__(self, other):
        return isinstance(other, Boolean) and self.value == other.value


class Object:
    TYPE = "java.lang.Object"


class Class:
    """Модель java.lang.Class: статический forName возвращает _FakeClass."""

    @staticmethod
    def forName(name):
        # Ленивая ссылка: default_find_class определён в конце модуля.
        return default_find_class(name)

    def __init__(self, instance=None):
        self._instance = instance


Boolean.TYPE = "boolean"
Integer.TYPE = "int"
Long.TYPE = "long"
java.lang.Boolean = Boolean
java.lang.Integer = Integer
java.lang.Long = Long
java.lang.Object = Object
java.lang.Class = Class

android = _mod("android")
android_view = _mod("android.view")


class _Gravity:
    LEFT = 3
    TOP = 80
    CENTER_VERTICAL = 16


class _MeasureSpec:
    AT_MOST = 1

    @staticmethod
    def makeMeasureSpec(size, mode):
        return (size, mode)


class View:
    VISIBLE = 0
    GONE = 8
    INVISIBLE = 4
    MeasureSpec = _MeasureSpec


android_view.Gravity = _Gravity
android_view.View = View
JView = View  # алиас для тестов

android_content = _mod("android.content")


class Context:
    pass


android_content.Context = Context


# ---------------------------------------------------------------------------
# base_plugin
# ---------------------------------------------------------------------------

base_plugin = _mod("base_plugin")


class HookStrategy:
    DEFAULT = 0
    CANCEL = 1
    MODIFY = 2
    MODIFY_FINAL = 3


@dataclass
class HookResult:
    strategy: int = HookStrategy.DEFAULT
    request: Any = None
    response: Any = None
    update: Any = None
    updates: Any = None
    params: Any = None


@dataclass
class AppEvent:
    START = 1
    STOP = 2
    PAUSE = 3
    RESUME = 4


@dataclass
class MenuItemData:
    menu_type: Any = None
    text: str = ""
    on_click: Optional[Callable] = None
    item_id: Optional[str] = None
    icon: Optional[str] = None
    subtext: Optional[str] = None
    condition: Optional[str] = None
    priority: int = 0


@dataclass
class MenuItemType:
    MESSAGE_CONTEXT_MENU = 0
    DRAWER_MENU = 1
    MAIN_MENU = 2
    CHAT_ACTION_MENU = 3
    PROFILE_ACTION_MENU = 4


class MethodHook:
    """Базовый обработчик Xposed-хука (как в SDK)."""

    def before_hooked_method(self, param):
        pass

    def after_hooked_method(self, param):
        pass


class MethodReplacement:
    def replace_hooked_method(self, param):
        return None


class HookFilter:
    @staticmethod
    def Condition(*a, **k):
        return None


def hook_filters(*a, **k):
    def deco(fn):
        return fn
    return deco


# Хуки конструктора SenderSelectPopup, которые «срабатывают» при создании
# попапа в stub-режиме (имитация Xposed-хука на конструктор).
POPUP_CTOR_HOOKS = []


class BasePlugin:
    _plugin_name = "stub"

    def __init__(self):
        self._settings = {}
        self._hooked_methods = []
        self._hooked_all_ctors = []
        self._send_msg_hook_added = False

    # lifecycle (переопределяются плагинами)
    def on_plugin_load(self):
        pass

    def on_plugin_unload(self):
        pass

    def create_settings(self):
        return []

    # helpers
    def log(self, message):
        print("[plugin:{}] {}".format(self._plugin_name, message))

    def getName(self):
        return self._plugin_name

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value, reload_settings=False):
        self._settings[key] = value

    def export_settings(self):
        return dict(self._settings)

    def import_settings(self, settings, reload_settings=True):
        self._settings.update(settings or {})

    def add_menu_item(self, data):
        return "menu_item_stub"

    def remove_menu_item(self, item_id):
        pass

    def hook_method(self, method, handler, priority=0):
        if getattr(method, "_is_popup_ctor", False):
            # Симуляция устройства: мост не умеет хукать отдельный
            # конструктор через hookMethod.
            raise RuntimeError(
                "hookMethod: constructor hooking not supported"
            )
        self._hooked_methods.append((method, handler))
        return ("hook_stub", method, handler)

    def unhook_method(self, obj):
        pass

    def add_hook(self, name, match_substring=False, priority=0):
        pass

    def hook_all_constructors(self, cls, handler, priority=0):
        self._hooked_all_ctors.append((cls, handler))
        if getattr(cls, "_name", "") == "org.telegram.ui.Components.SenderSelectPopup":
            POPUP_CTOR_HOOKS[:] = [handler]
        return ("hook_stub_all_ctors", cls, handler)

    def add_on_send_message_hook(self, priority=0):
        self._send_msg_hook_added = True

    def client(self, account=None):
        return None


base_plugin.HookStrategy = HookStrategy
base_plugin.HookResult = HookResult
base_plugin.AppEvent = AppEvent
base_plugin.MenuItemData = MenuItemData
base_plugin.MenuItemType = MenuItemType
base_plugin.MethodHook = MethodHook
base_plugin.MethodReplacement = MethodReplacement
base_plugin.HookFilter = HookFilter
base_plugin.hook_filters = hook_filters
base_plugin.BasePlugin = BasePlugin


# ---------------------------------------------------------------------------
# hook_utils
# ---------------------------------------------------------------------------

hook_utils = _mod("hook_utils")


class _FakeClass:
    """Модель java.lang.Class для тестов."""

    def __init__(self, name, methods=None, ctors=None):
        self._name = name
        self._methods = methods or {}
        self._ctors = ctors or {}

    def getName(self):
        return self._name

    def getDeclaredMethod(self, name, *param_types):
        key = (name, tuple(param_types))
        m = self._methods.get(key) or self._methods.get(name)
        if m is None:
            raise AttributeError("no method {} on {}".format(name, self._name))
        return m

    def getDeclaredMethods(self):
        return list(self._methods.values())

    def getDeclaredConstructor(self, *param_types):
        key = tuple(param_types)
        c = self._ctors.get(key)
        if c is None:
            raise AttributeError("no ctor on {}".format(self._name))
        return c

    def getDeclaredConstructors(self):
        return list(self._ctors.values())


class _FakeType:
    """Модель java.lang.Class (для getParameterTypes)."""

    def __init__(self, name):
        self._name = name

    def getName(self):
        return self._name


def _t(name):
    return _FakeType(name)


class _FakeMethod:
    def __init__(self, name, param_types, instance=None):
        self._name = name
        self._param_types = list(param_types)
        self._accessible = False
        self.impl = None
        self._is_popup_ctor = False

    def getName(self):
        return self._name

    def getParameterTypes(self):
        return self._param_types

    def setAccessible(self, v):
        self._accessible = bool(v)

    def invoke(self, obj, *args):
        if self.impl is not None:
            return self.impl(obj, *args)
        return None


def find_class(name):
    return default_find_class(name)


def get_private_field(obj, name):
    return getattr(obj, name, None)


hook_utils.find_class = find_class
hook_utils.get_private_field = get_private_field
hook_utils._FakeClass = _FakeClass
hook_utils._FakeMethod = _FakeMethod


# ---------------------------------------------------------------------------
# android_utils / client_utils
# ---------------------------------------------------------------------------

android_utils = _mod("android_utils")


class OnClickListener:
    def __init__(self, fn):
        self.fn = fn

    def onClick(self, view):
        self.fn(view)


class OnLongClickListener:
    def __init__(self, fn):
        self.fn = fn

    def onLongClick(self, view):
        return self.fn(view)


class R:
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        self.fn()


def log(data):
    pass


def run_on_ui_thread(func, delay=0):
    # В тестах выполняем сразу (синхронно).
    if delay:
        return
    func()


def copy_to_clipboard(text):
    return True


android_utils.OnClickListener = OnClickListener
android_utils.OnLongClickListener = OnLongClickListener
android_utils.R = R
android_utils.log = log
android_utils.run_on_ui_thread = run_on_ui_thread
android_utils.copy_to_clipboard = copy_to_clipboard

client_utils = _mod("client_utils")


class _Fragment:
    def getParentActivity(self):
        return "stub_activity"


_STUB_FRAGMENT = _Fragment()


def get_last_fragment():
    return _STUB_FRAGMENT


def get_notification_center():
    return None


def get_client(account=None):
    return None


def get_selected_account():
    return getattr(user_config, "selectedAccount", 0)


def send_request(req, fn, account=None):
    return 0


def run_on_queue(fn, *a, **k):
    fn()


client_utils.get_last_fragment = get_last_fragment
client_utils.get_notification_center = get_notification_center
client_utils.get_client = get_client
client_utils.get_selected_account = get_selected_account
client_utils.send_request = send_request
client_utils.run_on_queue = run_on_queue


# ---------------------------------------------------------------------------
# ui.*
# ---------------------------------------------------------------------------

ui = _mod("ui")
ui_settings = _mod("ui.settings")
ui_bulletin = _mod("ui.bulletin")
ui_alert = _mod("ui.alert")

ui.settings = ui_settings
ui.bulletin = ui_bulletin
ui.alert = ui_alert


class _SettingRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return "{}({})".format(type(self).__name__, self.__dict__)


def _row(cls_name):
    def __init__(self, **kw):
        self.__dict__.update(kw)
    cls = type(cls_name, (_SettingRow,), {"__init__": __init__, "__repr__": _SettingRow.__repr__})
    return cls


Header = _row("Header")
Divider = _row("Divider")
Switch = _row("Switch")
Selector = _row("Selector")
Input = _row("Input")
Text = _row("Text")
EditText = _row("EditText")
Custom = _row("Custom")
UItem = _row("UItem")


def SimpleSettingFactory(*a, **k):
    raise NotImplementedError


ui_settings.Header = Header
ui_settings.Divider = Divider
ui_settings.Switch = Switch
ui_settings.Selector = Selector
ui_settings.Input = Input
ui_settings.Text = Text
ui_settings.EditText = EditText
ui_settings.Custom = Custom
ui_settings.UItem = UItem
ui_settings.SimpleSettingFactory = SimpleSettingFactory


class BulletinHelper:
    SHOWN = []

    @classmethod
    def show_info(cls, text, fragment=None):
        cls.SHOWN.append(("info", text))

    @classmethod
    def show_error(cls, text, fragment=None):
        cls.SHOWN.append(("error", text))

    @classmethod
    def show_success(cls, text, fragment=None):
        cls.SHOWN.append(("success", text))

    @classmethod
    def show_with_button(cls, text, icon, button, on_click, fragment=None, duration=0):
        cls.SHOWN.append(("button", text, button))

    @classmethod
    def reset(cls):
        cls.SHOWN = []


ui_bulletin.BulletinHelper = BulletinHelper


class AlertDialogBuilder:
    ALERT_TYPE_MESSAGE = 0
    ALERT_TYPE_LOADING = 1
    ALERT_TYPE_SPINNER = 2
    BUTTON_POSITIVE = -1
    BUTTON_NEGATIVE = -2
    BUTTON_NEUTRAL = -3
    SHOWN = []

    def __init__(self, context, progress_style=0, resources_provider=None):
        self.context = context
        self.title = None
        self.message = None
        self.items = None
        self.item_listener = None
        self.positive = (None, None)
        self.negative = (None, None)
        self.shown = False

    def set_title(self, t):
        self.title = t

    def set_message(self, m):
        self.message = m

    def set_items(self, items, listener=None, icons=None):
        self.items = list(items)
        self.item_listener = listener

    def set_positive_button(self, text, listener=None):
        self.positive = (text, listener)

    def set_negative_button(self, text, listener=None):
        self.negative = (text, listener)

    def set_neutral_button(self, text, listener=None):
        pass

    def set_on_dismiss_listener(self, listener):
        pass

    def set_cancelable(self, v):
        pass

    def create(self):
        return self

    def show(self):
        self.shown = True
        AlertDialogBuilder.SHOWN.append(self)

    def dismiss(self):
        self.shown = False

    def click_item(self, index):
        if self.item_listener:
            self.item_listener(self, index)


ui_alert.AlertDialogBuilder = AlertDialogBuilder


# ---------------------------------------------------------------------------
# extera_utils.classes (class proxy DSL)
# ---------------------------------------------------------------------------

extera_utils = _mod("extera_utils")
extera_classes = _mod("extera_utils.classes")
extera_utils.classes = extera_classes


class Base:
    """Базовый Python-класс для управляемых Java-подклассов."""

    _java_obj = None

    @property
    def java(self):
        return self._java_obj

    @classmethod
    def new_instance(cls, *args, **kwargs):
        obj = cls.__new__(cls)
        if hasattr(cls, "_init_defaults"):
            for k, v in cls._init_defaults.items():
                if not hasattr(obj, k):
                    setattr(obj, k, v)
        obj._java_obj = obj  # в тестах java-объект совпадает с peer'ом
        return obj

    @classmethod
    def from_java(cls, java_obj):
        return getattr(java_obj, "_peer", java_obj)


def java_subclass(base_cls, *ifaces, **kwargs):
    def deco(cls):
        cls._java_base = base_cls
        cls._java_ifaces = ifaces
        defaults = {}
        for name in list(vars(cls)):
            v = vars(cls)[name]
            if isinstance(v, _JField):
                defaults[name] = v.default
        cls._init_defaults = defaults
        return cls
    return deco


class _JField:
    def __init__(self, type_str, default=None):
        self.type = type_str
        self.default = default


def jfield(type_str, default=None, methods=None):
    return _JField(type_str, default)


def joverride(*args, **kwargs):
    def deco(fn):
        fn._is_override = True
        return fn
    return deco


def joverload(name, arg_types, **kwargs):
    def deco(fn):
        fn._is_override = True
        return fn
    return deco


def jmethod(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


def jclassbuilder(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


def jconstructor(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


def jpreconstructor(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


def jgetmethod(*a, **k):
    return None


def jsetmethod(*a, **k):
    return None


class PyObj:
    @staticmethod
    def create(payload):
        return payload


extera_classes.Base = Base
extera_classes.java_subclass = java_subclass
extera_classes.joverride = joverride
extera_classes.joverload = joverload
extera_classes.jmethod = jmethod
extera_classes.jclassbuilder = jclassbuilder
extera_classes.jconstructor = jconstructor
extera_classes.jpreconstructor = jpreconstructor
extera_classes.jfield = jfield
extera_classes.jgetmethod = jgetmethod
extera_classes.jsetmethod = jsetmethod
extera_classes.jMVELmethod = jmethod
extera_classes.jMVELoverride = joverride
extera_classes.PyObj = PyObj


# ---------------------------------------------------------------------------
# org.telegram.*
# ---------------------------------------------------------------------------

def _pkg(name):
    return _mod(name)


org = _pkg("org")
telegram = _pkg("org.telegram")
tg_messenger = _pkg("org.telegram.messenger")
tg_tgnet = _pkg("org.telegram.tgnet")
tg_ui = _pkg("org.telegram.ui")
tg_ui_components = _pkg("org.telegram.ui.Components")
org.telegram = telegram
telegram.messenger = tg_messenger
telegram.tgnet = tg_tgnet
telegram.ui = tg_ui
tg_ui.Components = tg_ui_components


# --- TLRPC ---

class TLRPC:
    class TL_peerUser:
        def __init__(self):
            self.user_id = 0

    class TL_peerChannel:
        def __init__(self):
            self.channel_id = 0

    class TL_sendAsPeer:
        def __init__(self, peer=None, premium_required=False):
            self.peer = peer
            self.premium_required = premium_required

    class TL_channels_sendAsPeers:
        def __init__(self):
            self.peers = []
            self.chats = []
            self.users = []

    class TL_channels_joinChannel:
        def __init__(self):
            self.channel = None

    class ChatFull:
        def __init__(self, chat_id=0):
            self.id = chat_id
            self.default_send_as = None

    class TL_chat:
        def __init__(self, chat_id=0):
            self.id = chat_id
            self.title = "chat"
            self.kicked = False
            self.left = False
            self.deactivated = False
            self.banned_rights = None
            self.megagroup = False
            self.has_link = False
            self.participants_count = 0

    class TL_channel(TL_chat):
        def __init__(self, channel_id=0):
            super().__init__(channel_id)
            self.megagroup = True
            self.has_link = True


tg_tgnet.TLRPC = TLRPC


class ConnectionsManager:
    _instances = {}
    SENT = []

    @classmethod
    def getInstance(cls, num):
        if num not in cls._instances:
            cls._instances[num] = cls()
        return cls._instances[num]

    def sendRequest(self, req, cb, *args):
        ConnectionsManager.SENT.append((id(self), req, cb))
        return len(ConnectionsManager.SENT)

    @classmethod
    def reset(cls):
        cls.SENT = []
        cls._instances = {}


tg_tgnet.ConnectionsManager = ConnectionsManager


# --- UserConfig ---

class _User:
    def __init__(self, uid, first, last=""):
        self.id = uid
        self.first_name = first
        self.last_name = last


class _UserConfig:
    MAX_ACCOUNT_COUNT = 16
    selectedAccount = 0
    _users = {}

    def __init__(self, account):
        self.account = account

    def isClientActivated(self):
        return self.account in _UserConfig._users

    def getCurrentUser(self):
        return _UserConfig._users.get(self.account)

    @classmethod
    def getInstance(cls, num):
        return cls(num)

    @classmethod
    def isValidAccount(cls, num):
        return 0 <= num < cls.MAX_ACCOUNT_COUNT and num in cls._users

    @classmethod
    def setup(cls, accounts, selected=0):
        """accounts: {acc: (uid, name)}"""
        cls._users = {}
        for a, (uid, name) in accounts.items():
            parts = name.split(" ")
            cls._users[a] = _User(uid, parts[0], parts[1] if len(parts) > 1 else "")
        cls.selectedAccount = selected


user_config = _UserConfig
UserConfig = _UserConfig
tg_messenger.UserConfig = user_config


# --- MessagesController ---

class _NotificationsSettings:
    def getBoolean(self, key, default=False):
        return default

    def edit(self):
        return self

    def commit(self):
        pass

    def putBoolean(self, key, value):
        return self


class _MessagesController:
    _instances = {}

    def __init__(self, account):
        self.account = account
        self.chats = {}
        self.full_chats = {}
        self.default_send_as_calls = []

    @classmethod
    def getInstance(cls, num):
        if num not in cls._instances:
            cls._instances[num] = cls(num)
        return cls._instances[num]

    def getChat(self, chat_id):
        return self.chats.get(chat_id)

    def getChatFull(self, chat_id):
        return self.full_chats.get(chat_id)

    def putChat(self, chat_id, chat):
        self.chats[chat_id] = chat
        if chat_id not in self.full_chats:
            self.full_chats[chat_id] = TLRPC.ChatFull(chat_id)

    def getInputChannel(self, channel_id):
        p = TLRPC.TL_peerChannel()
        p.channel_id = channel_id
        return p

    def getInputPeer(self, peer_id):
        return peer_id

    def processUpdates(self, updates, background):
        pass

    def setDefaultSendAs(self, chat_id, new_peer):
        self.default_send_as_calls.append((chat_id, new_peer))

    @classmethod
    def getNotificationsSettings(cls, *a, **k):
        return _NotificationsSettings()

    @classmethod
    def reset(cls):
        cls._instances = {}


messages_controller = _MessagesController
MessagesController = _MessagesController
tg_messenger.MessagesController = messages_controller


# --- AccountInstance / SendMessagesHelper ---

class FakeParams:
    """Заглушка SendMessagesHelper.SendMessageParams."""


    def __init__(self, peer, retry=None):
        self.peer = peer
        self.retryMessageObject = retry


class _SendMessagesHelper:
    SENT = []

    def __init__(self, account):
        self.account = account
        self.currentAccount = account

    def sendMessage(self, *args):
        _SendMessagesHelper.SENT.append((self.account, len(args), args))


class _AccountInstance:
    _instances = {}

    def __init__(self, num):
        self.num = num
        self._send_helper = _SendMessagesHelper(num)

    @classmethod
    def getInstance(cls, num):
        if num not in cls._instances:
            cls._instances[num] = cls(num)
        return cls._instances[num]

    def getSendMessagesHelper(self):
        return self._send_helper

    def getMessagesController(self):
        return messages_controller.getInstance(self.num)

    @classmethod
    def reset(cls):
        cls._instances = {}
        _SendMessagesHelper.SENT = []


account_instance = _AccountInstance
tg_messenger.AccountInstance = account_instance
tg_messenger.SendMessagesHelper = _SendMessagesHelper


# --- ChatObject / DialogObject / UserObject / AndroidUtilities ---

class ChatObject:
    @staticmethod
    def isChannel(chat):
        return isinstance(chat, TLRPC.TL_channel)

    @staticmethod
    def getSendAsPeerId(chat, chat_full, invert=False):
        return 0


class DialogObject:
    @staticmethod
    def isEncryptedDialog(dialog_id):
        return (dialog_id & 0x4000000000000000) != 0 and (dialog_id & 0x8000000000000000) == 0

    @staticmethod
    def isUserDialog(dialog_id):
        return not DialogObject.isEncryptedDialog(dialog_id) and dialog_id > 0

    @staticmethod
    def isChatDialog(dialog_id):
        return not DialogObject.isEncryptedDialog(dialog_id) and dialog_id < 0


class UserObject:
    @staticmethod
    def getUserName(user):
        if user is None:
            return ""
        return (user.first_name + " " + user.last_name).strip()


class AndroidUtilities:
    statusBarHeight = 60


class NotificationCenter:
    updateDefaultSendAsPeer = 9000
    POSTED = []
    _instances = {}

    def __init__(self, num):
        self.num = num

    @classmethod
    def getInstance(cls, num):
        if num not in cls._instances:
            cls._instances[num] = cls(num)
        return cls._instances[num]

    def postNotificationName(self, notification_id, *args):
        NotificationCenter.POSTED.append((self.num, notification_id, args))

    @classmethod
    def reset(cls):
        cls.POSTED = []
        cls._instances = {}


tg_messenger.ChatObject = ChatObject
tg_messenger.DialogObject = DialogObject
tg_messenger.UserObject = UserObject
tg_messenger.AndroidUtilities = AndroidUtilities
tg_messenger.NotificationCenter = NotificationCenter


class _Raw:
    info = "raw_info"
    checkmark = "raw_checkmark"


class _R:
    raw = _Raw()


tg_messenger.R = _R


# --- LaunchActivity / ChatActivity ---

class LaunchActivity:
    instance = None

    def __init__(self):
        self.switched = []

    def switchToAccount(self, account, remove_all, provider=None):
        self.switched.append((account, remove_all))
        user_config.selectedAccount = account


class ChatActivity:
    pass


tg_ui.LaunchActivity = LaunchActivity
tg_ui.ChatActivity = ChatActivity


# --- RecyclerListView / SenderSelectPopup ---

class _RecyclerListView:
    class SelectionAdapter:
        def getItemCount(self):
            raise NotImplementedError

        def isEnabled(self, holder):
            raise NotImplementedError

        def onCreateViewHolder(self, parent, view_type):
            raise NotImplementedError

        def onBindViewHolder(self, holder, position):
            raise NotImplementedError

    class Holder:
        def __init__(self, item_view):
            self.itemView = item_view

    class OnItemClickListener:
        pass

    class OnItemLongClickListener:
        pass

    def __init__(self):
        self._adapter = None
        self._click = None
        self._long_click = None

    def getAdapter(self):
        return self._adapter

    def setAdapter(self, adapter):
        self._adapter = adapter

    def setOnItemClickListener(self, listener):
        self._click = listener

    def setOnItemLongClickListener(self, listener, duration=None):
        self._long_click = listener

    @property
    def onItemClickListener(self):
        return self._click

    @property
    def onItemLongClickListener(self):
        return self._long_click


class _SimpleAvatarView:
    def __init__(self):
        self.avatar_obj = None
        self._selected = False

    def setAvatar(self, obj):
        self.avatar_obj = obj

    def setSelected(self, selected, animate=False):
        self._selected = selected


class _SimpleText:
    def __init__(self, tag):
        self.tag = tag
        self._text = ""

    def setText(self, t):
        self._text = t

    def getText(self):
        return self._text


class _SenderView:
    def __init__(self, context=None, resources_provider=None):
        self.avatar = _SimpleAvatarView()
        self.title = _SimpleText("title")
        self.subtitle = _SimpleText("subtitle")
        self._alpha = 1.0
        self._selected = False

    def setAlpha(self, a):
        self._alpha = a

    def getAlpha(self):
        return self._alpha

    def setSelected(self, selected, animate=False):
        self._selected = selected

    def isSelected(self):
        return self._selected


class _OrigAdapter:
    """Имитация анонимного адаптера оригинального попапа."""

    def __init__(self, peers, controller, chat_full, popup):
        self.peers = peers
        self.controller = controller
        self.chat_full = chat_full
        self.popup = popup

    def getItemCount(self):
        return len(self.peers)

    def isEnabled(self, holder):
        return True

    def onCreateViewHolder(self, parent, view_type):
        return _RecyclerListView.Holder(_SenderView())

    def onBindViewHolder(self, holder, position):
        row = holder.itemView
        p = self.peers[position].peer
        if p.channel_id:
            chat = self.controller.getChat(p.channel_id)
            row.title.setText(chat.title if chat else "channel")
        return holder


class _OrigClick:
    def __init__(self, popup):
        self.popup = popup
        self.clicked = []

    default_send_as = None

    def onItemClick(self, view, position):
        self.clicked.append(position)
        peer = self.popup.sendAsPeers.peers[position].peer
        # Новая версия приложения запоминает выбранный отправитель
        # (в оригинале — chatFull.default_send_as + setDefaultSendAs).
        self.default_send_as = peer


class _PopupContent:
    def __init__(self):
        self._h = 200

    def measure(self, ws, hs):
        self._h = 200

    def getMeasuredHeight(self):
        return self._h


class SenderSelectPopup:
    SenderView = _SenderView
    LAST = None

    class OnSelectCallback:
        pass

    def __init__(self, context, parent_fragment, controller, is_channel, def_peer, send_as_peers, select_callback, resources_provider=None):
        self.context = context
        self.parentFragment = parent_fragment
        self.controller = controller
        self.defPeer = def_peer
        self.sendAsPeers = send_as_peers
        self.selectCallback = select_callback
        self.resourcesProvider = resources_provider
        self.recyclerView = _RecyclerListView()
        self.recyclerView._adapter = _OrigAdapter(send_as_peers.peers, controller, None, self)
        self.recyclerView._click = _OrigClick(self)
        self._dismissed = False
        SenderSelectPopup.LAST = self
        self._shown = False
        # Имитация Xposed-хука на конструктор.
        ctor_args = (
            context,
            parent_fragment,
            controller,
            is_channel,
            def_peer,
            send_as_peers,
            select_callback,
            resources_provider,
        )
        for h in list(POPUP_CTOR_HOOKS):
            h.after_hooked_method(_CtorParam(self, ctor_args))

    def setOutsideTouchable(self, v):
        pass

    def setFocusable(self, v):
        pass

    def setAnimationEnabled(self, v):
        pass

    def getContentView(self):
        return _PopupContent()

    def showAtLocation(self, anchor, gravity, x, y):
        self._shown = True

    def dismiss(self):
        self._dismissed = True


class _CtorParam:
    def __init__(self, this_object, args=None):
        self.thisObject = this_object
        self.args = args or []
        self.method = None

    def setResult(self, value):
        pass


class FakeChatActivity:
    """Имитация ChatActivity (родительский фрагмент попапа)."""

    def __init__(self, dialog_id):
        self._dialog_id = dialog_id

    def getDialogId(self):
        return self._dialog_id

    def getClass(self):
        return _FakeClass("org.telegram.ui.ChatActivity")


recycler_list_view = _RecyclerListView
sender_select_popup = SenderSelectPopup
tg_ui_components.RecyclerListView = recycler_list_view
tg_ui_components.SenderSelectPopup = sender_select_popup
tg_ui_components.ChatActivityEnterView = type("ChatActivityEnterView", (), {})


# ---------------------------------------------------------------------------
# find_class registry
# ---------------------------------------------------------------------------

_CONTEXT = _FakeClass("android.content.Context")
_CHAT_ACTIVITY = _FakeClass("org.telegram.ui.ChatActivity")
_MESSAGES_CONTROLLER = _FakeClass("org.telegram.messenger.MessagesController")
_TL_CHAT_FULL = _FakeClass("org.telegram.tgnet.TLRPC$ChatFull")
_TL_SEND_AS_PEERS = _FakeClass("org.telegram.tgnet.TLRPC$TL_channels_sendAsPeers")
_ON_SELECT_CALLBACK = _FakeClass("org.telegram.ui.Components.SenderSelectPopup$OnSelectCallback")
_BOOLEAN_TYPE = _FakeClass("boolean")
_TL_PEER = _FakeClass("org.telegram.tgnet.TLRPC$Peer")
_THEME_RP = _FakeClass("org.telegram.ui.ActionBar.Theme$ResourcesProvider")


def default_find_class(name):
    mapping = {
        "android.content.Context": _CONTEXT,
        "org.telegram.ui.ChatActivity": _CHAT_ACTIVITY,
        "org.telegram.messenger.MessagesController": _MESSAGES_CONTROLLER,
        "org.telegram.tgnet.TLRPC$ChatFull": _TL_CHAT_FULL,
        "org.telegram.tgnet.TLRPC$TL_channels_sendAsPeers": _TL_SEND_AS_PEERS,
        "org.telegram.ui.Components.SenderSelectPopup$OnSelectCallback": _ON_SELECT_CALLBACK,
        "boolean": _BOOLEAN_TYPE,
    }
    if name in mapping:
        return mapping[name]
    if name not in default_find_class._cache:
        cls = _FakeClass(name)
        if name == "org.telegram.ui.Components.SenderSelectPopup":
            ctor = _FakeMethod(
                "<init>",
                [
                    _CONTEXT,
                    _CHAT_ACTIVITY,
                    _MESSAGES_CONTROLLER,
                    _BOOLEAN_TYPE,
                    _TL_PEER,
                    _TL_SEND_AS_PEERS,
                    _ON_SELECT_CALLBACK,
                    _THEME_RP,
                ],
            )
            ctor._is_popup_ctor = True
            cls._ctors[(
                _CONTEXT,
                _CHAT_ACTIVITY,
                _MESSAGES_CONTROLLER,
                _BOOLEAN_TYPE,
                _TL_PEER,
                _TL_SEND_AS_PEERS,
                _ON_SELECT_CALLBACK,
                _THEME_RP,
            )] = ctor
        elif name == "org.telegram.ui.Components.ChatActivityEnterView":
            cls._methods["createSenderSelectView"] = _FakeMethod("createSenderSelectView", [])
            cls._methods["updateSendAsButton"] = _FakeMethod(
                "updateSendAsButton", [_BOOLEAN_TYPE]
            )
        elif name == "org.telegram.messenger.SendMessagesHelper":
            if SEND_API_ARCH == "new":
                # exteraGram 12.5.2+: весь трафик через SendMessageParams
                cls._methods["sendMessage_params"] = _FakeMethod(
                    "sendMessage",
                    [_t("org.telegram.messenger.SendMessagesHelper$SendMessageParams")],
                )
                cls._methods["sendMessage_7_new"] = _FakeMethod(
                    "sendMessage",
                    [_t("java.util.ArrayList"), _t("long"), _t("boolean"),
                     _t("boolean"), _t("boolean"), _t("int"), _t("long")],
                )
            else:
                cls._methods["sendMessage_27"] = _FakeMethod("sendMessage", [_t("x")] * 27)
                cls._methods["sendMessage_7"] = _FakeMethod(
                    "sendMessage",
                    [_t("java.util.ArrayList"), _t("long"), _t("boolean"),
                     _t("boolean"), _t("boolean"), _t("int"), _t("o")],
                )
        elif name == "org.telegram.tgnet.RequestCallback":
            pass
        default_find_class._cache[name] = cls
    return default_find_class._cache[name]


default_find_class._cache = {}


SEND_API_ARCH = "new"  # "new" = SendMessageParams (exteraGram 12.5.2+)


def set_send_api_arch(arch):
    """Симуляция старой/новой архитектуры SendMessagesHelper в тестах."""
    global SEND_API_ARCH
    SEND_API_ARCH = arch
    default_find_class._cache.pop(
        "org.telegram.messenger.SendMessagesHelper", None
    )
