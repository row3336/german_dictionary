"""Fake Telegram objects for the FSRS_sett inline message (send once, edit in place)."""
import itertools

_ids = itertools.count(100)


class Msg:
    def __init__(self, text="", chat_id=5):
        self.text, self.chat_id, self.message_id = text, chat_id, next(_ids)
        self.sent, self.markup = [], None

    async def reply_text(self, text, reply_markup=None, **kw):
        sent = Msg(text, self.chat_id)
        sent.markup = reply_markup
        self.sent.append(sent)
        return sent


class Query:
    def __init__(self, data, uid, message, fail_edit=None):
        self.data, self.message, self.from_user = data, message, type("U", (), {"id": uid})()
        self.edits, self.acks, self.fail_edit = [], 0, fail_edit

    async def answer(self, *a, **k):
        self.acks += 1

    async def edit_message_text(self, text, reply_markup=None, **kw):
        if self.fail_edit:
            raise self.fail_edit
        self.message.text, self.message.markup = text, reply_markup
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.message.markup = reply_markup


class Ctx:
    def __init__(self):
        self.user_data = {}


def entry_update(text, uid):
    return type("Upd", (), {"message": Msg(text), "effective_user": type("U", (), {"id": uid})()})()


def press(handler, ctx, uid, data, message, **kw):
    query = Query(data, uid, message, **kw)
    state = __import__("asyncio").run(handler(type("Upd", (), {"callback_query": query})(), ctx))
    return state, query


def buttons(message):
    return [b.callback_data for row in message.markup.inline_keyboard for b in row]
