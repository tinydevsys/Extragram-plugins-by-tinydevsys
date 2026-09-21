# -*- coding: utf-8 -*-
"""Desktop-тесты логики плагина send_as_accounts (без приложения).

Запуск:  python3 tests/test_logic.py
"""

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, HERE)
import stubs  # noqa: F401,E402  (устанавливает все stub-модули)
import stubs as S  # noqa: E402

from stubs import (  # noqa: E402
    AlertDialogBuilder,
    BulletinHelper,
    LaunchActivity,
    MessagesController,
    SenderSelectPopup,
    TLRPC,
    UserConfig,
    _FakeMethod,
    _SendMessagesHelper,
)


# ---------------------------------------------------------------------------
# Импорт плагина
# ---------------------------------------------------------------------------

def _load_plugin_module():
    path = os.path.join(ROOT, "send_as_accounts.py")
    spec = importlib.util.spec_from_file_location("send_as_accounts_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_plugin_module()
PluginCls = MOD.SendAsAccountsPlugin


def make_plugin():
    p = PluginCls()
    p.on_plugin_load()
    return p


def setup_accounts():
    """Аккаунты: 0=Alice (текущий), 1=Bob, 2=Carol, 3=Dave."""
    UserConfig.setup(
        {
            0: (100, "Alice Smith"),
            1: (101, "Bob Jones"),
            2: (102, "Carol White"),
            3: (103, "Dave Brown"),
        },
        selected=0,
    )


def setup_group_chat(chat_id=777, megagroup=True, has_link=True):
    """Группа -chat_id: Alice и Bob участники, Carol исключена, Dave забанен."""
    for acc in (0, 1, 2, 3):
        mc = MessagesController.getInstance(acc)
        chat = TLRPC.TL_channel(chat_id)
        chat.title = "Тестовая группа"
        chat.megagroup = megagroup
        chat.has_link = has_link
        if acc == 2:
            chat.kicked = True
        if acc == 3:
            chat.banned_rights = types.SimpleNamespace(view_messages=True)
        mc.putChat(chat_id, chat)
    return chat_id


# ---------------------------------------------------------------------------
# Фейк param для Xposed-хуков
# ---------------------------------------------------------------------------

class FakeParam:
    def __init__(self, this_object=None, args=None, method=None):
        self.thisObject = this_object
        self.args = args
        self.method = method
        self.result_set = "UNSET"

    def setResult(self, value):
        self.result_set = value


PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  ok: {}".format(name))
    else:
        FAIL.append(name)
        print("FAIL: {} {}".format(name, extra))


def last_bulletin():
    if not BulletinHelper.SHOWN:
        return None
    return BulletinHelper.SHOWN[-1]


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

def test_metadata():
    print("[metadata]")
    import ast
    import re
    path = os.path.join(ROOT, "send_as_accounts.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    consts = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if name.startswith("__") and name.endswith("__"):
                try:
                    consts[name] = ast.literal_eval(node.value)
                except Exception:
                    pass
    check("id", consts.get("__id__") == "send_as_accounts")
    check("name", bool(consts.get("__name__")))
    check("version", consts.get("__version__") == "1.0.2")
    check("app_version", "12.5.1" in consts.get("__app_version__", ""))
    check("sdk_version", "1.4.4.3" in consts.get("__sdk_version__", ""))
    check("icon", consts.get("__icon__", "").startswith("exteraPlugins"))
    check("id format", bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,31}", consts.get("__id__", ""))))


def test_status():
    print("[status]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    check("member", p._account_status(-777, 0) == ("member", "в чате"))
    check("member2", p._account_status(-777, 1) == ("member", "в чате"))
    check("kicked", p._account_status(-777, 2) == ("kicked", "исключён"))
    check("banned", p._account_status(-777, 3) == ("banned", "забанен"))
    MessagesController.getInstance(1).getChat(777).left = True
    check("left", p._account_status(-777, 1) == ("out", "не в чате"))
    MessagesController.getInstance(1).getChat(777).left = False
    check("unknown", p._account_status(-999, 1) == ("unknown", "не в чате"))
    check("private", p._account_status(555, 1)[0] == "member")


def test_order_and_hidden():
    print("[order/hidden]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    check("order default", p._get_order() == [0, 1, 2, 3])
    p.set_setting("account_order", [2, 0, 3, 1])
    check("order saved", p._get_order() == [2, 0, 3, 1])
    p.set_setting("account_order", [9])
    check("order invalid filtered", p._get_order() == [0, 1, 2, 3])
    p.set_setting("account_order", [0, 1, 2, 3])
    p._move_account(3, -1)
    check("move up", p._get_order() == [0, 1, 3, 2])
    p._move_account(3, +1)
    check("move down", p._get_order() == [0, 1, 2, 3])
    p._move_account(0, -1)
    check("move up at top no-op", p._get_order() == [0, 1, 2, 3])
    check("no hidden", len(p._get_accounts_for_list()) == 4)
    p.set_setting("hidden_accounts", {"1": True})
    check("hidden", p._get_accounts_for_list() == [0, 2, 3])
    p.set_setting("manual_hidden_channels", "123, -456 ; 789")
    check("manual hidden ids", p._manual_hidden_channel_ids() == {"123", "-456", "789"})


def test_sender_map():
    print("[sender map]")
    p = make_plugin()
    setup_accounts()
    check("no sender", p.get_sender_account(-777) is None)
    p.set_setting("remember_sender", True)
    p._apply_sender(-777, 1)
    check("sender saved", p.get_sender_account(-777) == 1)
    check("map persisted", p.get_setting("chat_senders") == {"-777": 1})
    p._clear_sender(-777)
    check("sender cleared", p.get_sender_account(-777) is None)
    p.set_setting("remember_sender", False)
    p._apply_sender(-777, 2)
    check("sender mem", p.get_sender_account(-777) == 2)
    check("map not persisted", p.get_setting("chat_senders", {}).get("-777") is None)
    p._mem_senders = {"-1": 99}
    check("invalid acc", p.get_sender_account(-1) is None)


def test_apply_sender_modes():
    print("[modes]")
    la = LaunchActivity()
    LaunchActivity.instance = la
    p = make_plugin()
    setup_accounts()
    setup_group_chat()

    p.set_setting("switch_mode", MOD.MODE_SWITCH)
    p._apply_sender(-777, 1)
    check("switch mode group: saved", p.get_sender_account(-777) == 1)
    check("switch mode group: no app switch", la.switched == [])
    check(
        "switch mode group: bulletin",
        last_bulletin() is not None
        and last_bulletin()[0] == "info"
        and "Отправлять от" in last_bulletin()[1],
    )

    full = MessagesController.getInstance(0).getChatFull(777)
    check(
        "default_send_as set",
        full.default_send_as is not None and full.default_send_as.user_id == 101,
    )

    p._apply_sender(-777, 0)
    check("select current resets", p.get_sender_account(-777) is None)
    check(
        "default_send_as reset",
        MessagesController.getInstance(0).getChatFull(777).default_send_as is None,
    )

    p.set_setting("switch_mode", MOD.MODE_FULL)
    p._apply_sender(-777, 2)
    check("full mode: switched", la.switched == [(2, True)])
    check("full mode: not saved", p.get_sender_account(-777) is None)

    p.set_setting("switch_mode", MOD.MODE_HYBRID)
    p._apply_sender(-777, 1)
    check("hybrid group: saved", p.get_sender_account(-777) == 1)
    check("hybrid group: no switch", la.switched == [(2, True)])
    p._clear_sender(-777)
    p._apply_sender(555, 1)
    check("hybrid private: switched", la.switched == [(2, True), (1, True)])

    # после переключения текущий аккаунт = 1, выбираем 2
    enc = 0x4000000000000001
    p._apply_sender(enc, 2)
    check("hybrid encrypted: saved (no switch)", p.get_sender_account(enc) == 2)


def test_send_redirect_official():
    print("[send redirect: official hook (SendMessageParams)]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    BulletinHelper.reset()
    p.set_setting("switch_mode", MOD.MODE_SWITCH)

    check("official hook registered", p._send_msg_hook_added is True)
    send_counts = [
        len(m.getParameterTypes())
        for m, _h in p._hooked_methods
        if m.getName() == "sendMessage"
    ]
    check(
        "new 7-arg (...,long) not Xposed-hooked",
        7 not in send_counts and 27 not in send_counts,
        str(send_counts),
    )

    p._apply_sender(-777, 1)
    check("precondition", p.get_sender_account(-777) == 1)

    _SendMessagesHelper.SENT.clear()
    params = S.FakeParams(peer=-777)
    res = p.on_send_message_hook(0, params)
    check("result CANCEL", res is not None and res.strategy == S.HookStrategy.CANCEL)
    check(
        "sent via account 1",
        len(_SendMessagesHelper.SENT) == 1 and _SendMessagesHelper.SENT[0][0] == 1,
        str(_SendMessagesHelper.SENT),
    )
    check(
        "same params object",
        _SendMessagesHelper.SENT and _SendMessagesHelper.SENT[0][2][0] is params,
    )
    check(
        "sent-as bulletin",
        last_bulletin() is not None and last_bulletin()[0] == "info"
        and "Отправлено от" in last_bulletin()[1],
    )

    _SendMessagesHelper.SENT.clear()
    res2 = p.on_send_message_hook(1, params)
    check(
        "no re-redirect from target",
        res2.strategy == S.HookStrategy.DEFAULT and not _SendMessagesHelper.SENT,
    )

    _SendMessagesHelper.SENT.clear()
    res3 = p.on_send_message_hook(0, S.FakeParams(peer=-777, retry=object()))
    check(
        "retry not redirected",
        res3.strategy == S.HookStrategy.DEFAULT and not _SendMessagesHelper.SENT,
    )

    p._clear_sender(-777)
    _SendMessagesHelper.SENT.clear()
    res4 = p.on_send_message_hook(0, params)
    check(
        "no sender -> original send",
        res4.strategy == S.HookStrategy.DEFAULT and not _SendMessagesHelper.SENT,
    )

    p._apply_sender(555, 1)
    _SendMessagesHelper.SENT.clear()
    BulletinHelper.reset()
    res5 = p.on_send_message_hook(0, S.FakeParams(peer=555))
    check(
        "private redirected",
        res5.strategy == S.HookStrategy.CANCEL
        and _SendMessagesHelper.SENT and _SendMessagesHelper.SENT[0][0] == 1,
    )
    check(
        "private bulletin with switch button",
        last_bulletin() is not None and last_bulletin()[0] == "button"
        and "Переключиться" in last_bulletin()[2],
        str(last_bulletin()),
    )

    p._clear_sender(555)


def test_send_redirect_legacy():
    print("[send redirect: legacy Xposed (old API)]")
    S.set_send_api_arch("old")
    try:
        p = make_plugin()
        setup_accounts()
        setup_group_chat()
        BulletinHelper.reset()
        p.set_setting("switch_mode", MOD.MODE_SWITCH)

        check("official hook NOT registered", p._send_msg_hook_added is False)
        send_counts = sorted(
            len(m.getParameterTypes())
            for m, _h in p._hooked_methods
            if m.getName() == "sendMessage"
        )
        check("legacy 27+7 hooked", send_counts == [7, 27], str(send_counts))

        args27 = [None] * 27
        args27[0] = "hello"
        args27[10] = -777
        helper_a0 = _SendMessagesHelper(0)

        calls = []
        method = _FakeMethod("sendMessage", ["x"] * 27)
        method.impl = lambda obj, arr: calls.append((obj.account, tuple(arr))) or obj.sendMessage(*arr)

        p._apply_sender(-777, 1)
        check("precondition", p.get_sender_account(-777) == 1)

        param = FakeParam(this_object=helper_a0, args=args27, method=method)
        p._on_send_before(param)
        check("original skipped", param.result_set is None)
        check("redirect happened", len(calls) == 1, str(calls))
        if calls:
            check("redirect to account 1", calls[0][0] == 1)
            check(
                "boxed long",
                isinstance(calls[0][1][10], S.java.lang.Long) and int(calls[0][1][10]) == -777,
            )

        BulletinHelper.reset()
        p._on_send_after(param)
        check(
            "sent-as bulletin",
            last_bulletin() is not None
            and last_bulletin()[0] == "info"
            and "Отправлено от" in last_bulletin()[1],
        )

        BulletinHelper.reset()
        p._on_send_after(FakeParam(this_object=helper_a0, args=args27, method=method))
        check("no duplicate bulletin", len(BulletinHelper.SHOWN) == 0)

        p._clear_sender(-777)
        calls.clear()
        param2 = FakeParam(this_object=helper_a0, args=args27, method=method)
        p._on_send_before(param2)
        check("no redirect without sender", param2.result_set == "UNSET" and len(calls) == 0)

        p._apply_sender(-777, 1)
        args_retry = list(args27)
        args_retry[16] = object()
        calls.clear()
        param3 = FakeParam(this_object=helper_a0, args=args_retry, method=method)
        p._on_send_before(param3)
        check("retry not redirected", len(calls) == 0 and param3.result_set == "UNSET")

        args7 = [None, -777, True, False, True, 0, None]
        calls.clear()
        method7 = _FakeMethod("sendMessage", ["a", "long", "b", "b", "b", "i", "o"])
        method7.impl = lambda obj, arr: calls.append((obj.account, tuple(arr))) or obj.sendMessage(*arr)
        param4 = FakeParam(this_object=helper_a0, args=args7, method=method7)
        p._on_send_before(param4)
        check("forward redirected", len(calls) == 1 and calls[0][0] == 1, str(calls))

        p._apply_sender(555, 1)
        args27b = list(args27)
        args27b[10] = 555
        calls.clear()
        param5 = FakeParam(this_object=helper_a0, args=args27b, method=method)
        p._on_send_before(param5)
        check("private redirected", len(calls) == 1 and calls[0][0] == 1)
        BulletinHelper.reset()
        p._on_send_after(param5)
    finally:
        S.set_send_api_arch("new")


def test_validate_target():
    print("[validate target]")
    BulletinHelper.reset()
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    check("member ok", p._validate_target(-777, 0, 1) == 1)
    check("kicked blocked", p._validate_target(-777, 0, 2) is None)
    check(
        "kicked bulletin",
        last_bulletin() is not None
        and last_bulletin()[0] == "error"
        and "исключён" in last_bulletin()[1],
    )
    BulletinHelper.reset()
    check("banned blocked", p._validate_target(-777, 0, 3) is None)
    check(
        "banned bulletin",
        last_bulletin() is not None
        and last_bulletin()[0] == "error"
        and "заблокирован" in last_bulletin()[1],
    )
    check("private ok", p._validate_target(555, 0, 1) == 1)


def test_popup_injection():
    print("[popup injection]")
    BulletinHelper.reset()
    p = make_plugin()
    # Мост симулирует сломанный hookMethod для конструктора: плагин должен
    # пройти по фолбэку hook_all_constructors.
    check("popup fallback used", len(p._hooked_all_ctors) == 1, str(p._hooked_all_ctors))
    diag_text = "\n".join(p._diag_log)
    check("popup fallback diag", "ok (all ctors)" in diag_text, diag_text)
    setup_accounts()
    chat_id = setup_group_chat()

    peer_ch1 = TLRPC.TL_peerChannel(); peer_ch1.channel_id = 9001
    peer_ch2 = TLRPC.TL_peerChannel(); peer_ch2.channel_id = 9002
    mc0 = MessagesController.getInstance(0)
    c1 = TLRPC.TL_channel(9001); c1.title = "Канал 1"
    c2 = TLRPC.TL_channel(9002); c2.title = "Канал 2"
    mc0.putChat(9001, c1)
    mc0.putChat(9002, c2)

    send_as = TLRPC.TL_channels_sendAsPeers()
    send_as.peers = [TLRPC.TL_sendAsPeer(peer_ch1), TLRPC.TL_sendAsPeer(peer_ch2)]
    chat_full = TLRPC.ChatFull(chat_id)

    # При создании попапа «срабатывает» хук на конструктор.
    # Порядок: сначала 4 аккаунта (позиции 0-3), потом 2 канала (4-5).
    popup = SenderSelectPopup(None, None, mc0, chat_full, send_as, None)
    recycler = popup.recyclerView
    adapter = recycler.getAdapter()
    check("adapter replaced", type(adapter) is MOD.SenderAdapter)
    check("count = 4 acc + 2 ch", adapter.getItemCount() == 6, str(adapter.getItemCount()))
    check("viewtype account first", adapter.getItemViewType(0) == 1)
    check("viewtype channel last", adapter.getItemViewType(4) == 0)

    seen = p.get_setting("seen_channels", {})
    check("seen channels", seen.get("9001") == "Канал 1" and seen.get("9002") == "Канал 2")

    row = adapter.onCreateViewHolder(None, 1)
    adapter.onBindViewHolder(row, 0)
    check("row title current", "Alice" in row.itemView.title.getText())
    check("row not greyed", row.itemView.getAlpha() == 1.0)

    # канал на позиции 4 (оригинальный индекс 0)
    row_ch = adapter.onCreateViewHolder(None, 0)
    adapter.onBindViewHolder(row_ch, 4)
    check("channel row via orig adapter", row_ch.itemView.title.getText() == "Канал 1")

    pos_carol = [0, 1, 2, 3].index(2)  # Carol = accounts[2], позиция 2
    row2 = adapter.onCreateViewHolder(None, 1)
    adapter.onBindViewHolder(row2, pos_carol)
    check("kicked greyed", row2.itemView.getAlpha() == 0.5)
    check("kicked status", row2.itemView.subtitle.getText() == "исключён")

    # state, в котором лежит оригинальный слушатель
    state = None
    for k, v in MOD.STATE_REGISTRY.items():
        if v.get("popup") is popup:
            state = v
            break
    check("state registered", state is not None)

    # клик по каналу (позиция 4) -> делегирование + сброс отправителя
    p._apply_sender(-chat_id, 1)
    recycler.onItemClickListener.onItemClick(None, 4)
    check("orig click delegated", len(state["orig_listener"].clicked) == 1)
    check("sender cleared on channel select", p.get_sender_account(-chat_id) is None)
    check("chatFull default set to channel", chat_full.default_send_as is peer_ch1)

    # клик по Bob (позиция 1)
    recycler.onItemClickListener.onItemClick(None, 1)
    check("bob selected", p.get_sender_account(-chat_id) == 1)
    check(
        "selection bulletin",
        last_bulletin() is not None
        and last_bulletin()[0] == "info"
        and "Отправлять от «Bob" in last_bulletin()[1],
    )

    # клик по текущему аккаунту (позиция 0) — сброс
    recycler.onItemClickListener.onItemClick(None, 0)
    check("current resets", p.get_sender_account(-chat_id) is None)

    # клик по kicked (Carol, позиция 2) — предложение вступить (диалог, не bulletin)
    BulletinHelper.reset()
    AlertDialogBuilder.SHOWN = []
    recycler.onItemClickListener.onItemClick(None, pos_carol)
    check("kicked -> no sender", p.get_sender_account(-chat_id) is None)
    check("kicked -> join dialog", len(AlertDialogBuilder.SHOWN) == 1)
    check("no bulletin for join", len(BulletinHelper.SHOWN) == 0)

    # долгое нажатие на Bob (позиция 1) — полная смена
    la = LaunchActivity()
    LaunchActivity.instance = la
    recycler.onItemLongClickListener.onItemClick(None, 1)
    check("long press switches app", la.switched == [(1, True)])

    # hide channels
    p2 = make_plugin()
    setup_accounts()
    p2.set_setting("hide_channels", True)
    popup2 = SenderSelectPopup(None, None, mc0, TLRPC.ChatFull(chat_id), send_as, None)
    a2 = popup2.recyclerView.getAdapter()
    check("hide channels: accounts only", a2.getItemCount() == 4, str(a2.getItemCount()))

    # скрыть конкретный канал
    p3 = make_plugin()
    setup_accounts()
    p3.set_setting("hidden_channels", {"9001": True})
    popup3 = SenderSelectPopup(None, None, mc0, TLRPC.ChatFull(chat_id), send_as, None)
    a3 = popup3.recyclerView.getAdapter()
    check("hidden channel: 1 orig + 4 acc", a3.getItemCount() == 5, str(a3.getItemCount()))


def test_private_popup():
    print("[private popup]")
    p = make_plugin()
    setup_accounts()

    class Anchor:
        def getLocationInWindow(self, loc):
            loc[0], loc[1] = 10, 600

        def setProgress(self, v):
            pass

    class EnterView:
        parentFragment = None
        dialog_id = 555

        def getContext(self):
            return None

    p._show_own_popup(Anchor(), EnterView(), 555)
    check("no pending left", p._pending_popup_peer is None)
    popup = SenderSelectPopup.LAST
    a = popup.recyclerView.getAdapter()
    check(
        "private popup accounts",
        type(a) is MOD.SenderAdapter and a.getItemCount() == 4,
    )


def test_chat_menu():
    print("[chat menu]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    AlertDialogBuilder.SHOWN = []

    p._on_chat_action_menu({"dialog_id": -777})
    dialogs = AlertDialogBuilder.SHOWN
    check("dialog shown", len(dialogs) == 1)
    if dialogs:
        d = dialogs[0]
        check("dialog items", len(d.items) == 4, str(d.items))
        check("kicked in items", "исключён" in d.items[2])
        d.click_item(1)
        check("menu select bob", p.get_sender_account(-777) == 1)

    BulletinHelper.reset()
    c = TLRPC.TL_channel(888)
    c.megagroup = False
    MessagesController.getInstance(0).putChat(888, c)
    p._on_chat_action_menu({"dialog_id": -888})
    check(
        "channel blocked",
        last_bulletin() is not None
        and last_bulletin()[0] == "info"
        and "Недоступно" in last_bulletin()[1],
    )

    BulletinHelper.reset()
    enc = 0x4000000000000001
    p._on_chat_action_menu({"dialog_id": enc})
    check(
        "encrypted blocked",
        last_bulletin() is not None
        and last_bulletin()[0] == "info"
        and "секретных" in last_bulletin()[1],
    )


def test_join_flow():
    print("[join flow]")
    p = make_plugin()
    setup_accounts()
    chat_id = setup_group_chat(has_link=True)
    BulletinHelper.reset()
    AlertDialogBuilder.SHOWN = []
    S.ConnectionsManager.reset()

    p._offer_join(-chat_id, 2)
    dialogs = AlertDialogBuilder.SHOWN
    check("join dialog", len(dialogs) == 1 and dialogs[0].title == "Вступить в чат?")
    if dialogs:
        text, listener = dialogs[0].positive
        check("positive is join", text == "Вступить")
        listener(dialogs[0], -1)

    check("join request sent", len(S.ConnectionsManager.SENT) == 1)
    if S.ConnectionsManager.SENT:
        _, req, cb = S.ConnectionsManager.SENT[0]
        check("join req channel", req.channel.channel_id == chat_id)
        state_tokens = [k for k, v in MOD.STATE_REGISTRY.items() if "on_result" in v]
        check("cb state", len(state_tokens) >= 1)
        cb.run(object(), None)
        check(
            "join success bulletin",
            last_bulletin() is not None and last_bulletin()[0] == "success",
        )

    setup_group_chat(chat_id=778, has_link=False)
    BulletinHelper.reset()
    p._offer_join(-778, 2)
    check(
        "no link -> info",
        last_bulletin() is not None
        and last_bulletin()[0] == "info"
        and "ссылки" in last_bulletin()[1],
    )


def test_settings():
    print("[settings]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    rows = p.create_settings()
    keys = [r.key for r in rows if hasattr(r, "key")]
    for expected in [
        "switch_mode", "long_press_switch", "hide_channels",
        "show_sent_as", "remember_sender", "show_status",
    ]:
        check("setting: " + expected, expected in keys)
    sel = [r for r in rows if getattr(r, "key", None) == "switch_mode"][0]
    check("selector 3 items", len(sel.items) == 3)
    check("default mode is hybrid", sel.default == MOD.MODE_HYBRID)

    rows_acc = p._accounts_hidden_subpage()
    check(
        "accounts subpage",
        any(r.key == "hidden_account_1" for r in rows_acc if hasattr(r, "key")),
    )
    rows_ch = p._channels_hidden_subpage()
    check(
        "channels subpage input",
        any(r.key == "manual_hidden_channels" for r in rows_ch if hasattr(r, "key")),
    )
    rows_order = p._order_subpage()
    check("order subpage", len(rows_order) >= 6)

    # перемещение через UI: вторая строка (acc 1) -> наверх
    order_rows = [r for r in rows_order if getattr(r, "text", "").startswith("2.")]
    check("order row found", len(order_rows) == 1)
    order_rows[0].on_click(None)
    check("order moved via ui", p._get_order()[0] == 1, str(p._get_order()))


def test_sender_view_wrap():
    print("[sender view wrap]")
    p = make_plugin()
    setup_accounts()

    class FakeEnterView:
        dialog_id = 555
        senderSelectView = None
        delegate = None

        def getContext(self):
            return None

    ev = FakeEnterView()

    class FakeView:
        def __init__(self):
            self._tag = None
            self._listener = S.OnClickListener(lambda v: None)
            self._wrapped = None

        def getTag(self):
            return self._tag

        def setTag(self, t):
            self._tag = t

        def getOnClickListener(self):
            return self._listener

        def setOnClickListener(self, l):
            self._wrapped = l

    ev.senderSelectView = FakeView()
    param = FakeParam(this_object=ev)
    p._on_sender_view_created(param)
    check("wrapped once", ev.senderSelectView._wrapped is not None)
    check("tag set", ev.senderSelectView.getTag() == MOD.VIEW_TAG_WRAPPED)
    before = ev.senderSelectView._wrapped
    p._on_sender_view_created(param)
    check("not double wrapped", ev.senderSelectView._wrapped is before)

    tracker = {"created": 0}
    orig_show = p._show_own_popup

    def _track(*a, **k):
        tracker["created"] += 1

    p._show_own_popup = _track
    ev.senderSelectView._wrapped.onClick(None)
    check("private click shows popup", tracker["created"] == 1)
    p._show_own_popup = orig_show


def test_update_send_as_avatar():
    print("[update send as avatar]")
    p = make_plugin()
    setup_accounts()
    setup_group_chat()
    p._apply_sender(-777, 1)

    class FakeEnterView:
        dialog_id = -777
        senderSelectView = None

    ev = FakeEnterView()

    class FakeView:
        def __init__(self):
            self.avatar_obj = None
            self.visible = None

        def setAvatar(self, u):
            self.avatar_obj = u

        def setVisibility(self, v):
            self.visible = v

    ev.senderSelectView = FakeView()
    param = FakeParam(this_object=ev)
    p._on_update_send_as_after(param)
    check(
        "avatar set",
        ev.senderSelectView.avatar_obj is not None
        and ev.senderSelectView.avatar_obj.id == 101,
    )
    check("visible", ev.senderSelectView.visible == S.JView.VISIBLE)


def test_private_button():
    print("[private button]")
    p = make_plugin()
    setup_accounts()
    S.NotificationCenter.reset()
    p.set_setting("switch_mode", MOD.MODE_SWITCH)
    p._apply_sender(555, 1)
    check("private sender saved", p.get_sender_account(555) == 1)
    posted = [
        args for (_, nid, args) in S.NotificationCenter.POSTED
        if nid == S.NotificationCenter.updateDefaultSendAsPeer
    ]
    check("send-as notification posted", any(a[0] == 555 for a in posted))

    # Приложение вызывает updateSendAsButton -> after-хук создаёт/показывает кнопку
    class _Anim:
        def cancel(self):
            pass

    class FakeView:
        def __init__(self):
            self.avatar_obj = None
            self.visible = None
            self._tag = _Anim()

        def getTag(self):
            return self._tag

        def setTag(self, t):
            self._tag = t

        def setAvatar(self, u):
            self.avatar_obj = u

        def setVisibility(self, v):
            self.visible = v

        def setAlpha(self, a):
            pass

        def setTranslationX(self, x):
            pass

    class FakeEnterView:
        dialog_id = 555
        senderSelectView = FakeView()

    ev = FakeEnterView()
    p._on_update_send_as_after(FakeParam(this_object=ev))
    check(
        "private button avatar",
        ev.senderSelectView.avatar_obj is not None
        and ev.senderSelectView.avatar_obj.id == 101,
    )
    check("private button visible", ev.senderSelectView.visible == S.JView.VISIBLE)
    check("animator cancelled", ev.senderSelectView.getTag() is None)

    # Сброс отправителя -> уведомление на скрытие
    S.NotificationCenter.reset()
    p._clear_sender(555)
    posted = [
        args for (_, nid, args) in S.NotificationCenter.POSTED
        if nid == S.NotificationCenter.updateDefaultSendAsPeer
    ]
    check("hide notification posted", any(a[0] == 555 for a in posted))


def test_diag():
    print("[diag]")
    p = make_plugin()
    setup_accounts()
    check("hooks tracked", p._hooks_ok == p._hooks_total == 4, str(p._diag_log))
    check("diag log nonempty", len(p._diag_log) >= 4)
    p._warn_once("k1", "тестовая ошибка")
    n1 = len(BulletinHelper.SHOWN)
    p._warn_once("k1", "тестовая ошибка 2")
    check("warn once", len(BulletinHelper.SHOWN) == n1)
    AlertDialogBuilder.SHOWN = []
    p._show_diag()
    check("diag dialog shown", len(AlertDialogBuilder.SHOWN) == 1)
    if AlertDialogBuilder.SHOWN:
        check("diag dialog content", "Хуки" in AlertDialogBuilder.SHOWN[0].message)


def main():
    test_metadata()
    test_status()
    test_order_and_hidden()
    test_sender_map()
    test_apply_sender_modes()
    test_send_redirect_official()
    test_send_redirect_legacy()
    test_validate_target()
    test_popup_injection()
    test_private_popup()
    test_chat_menu()
    test_join_flow()
    test_settings()
    test_sender_view_wrap()
    test_update_send_as_avatar()
    test_private_button()
    test_diag()

    print()
    print("PASS: {}  FAIL: {}".format(len(PASS), len(FAIL)))
    if FAIL:
        print("Failed tests:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
