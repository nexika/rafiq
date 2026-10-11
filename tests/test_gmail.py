import base64
import email

import pytest

from rafiq.tools import REGISTRY, google, load_all


class FakeGmail:
    """Stands in for the Gmail API client: records what was sent or trashed."""

    def __init__(self, message: dict):
        self.message, self.sent, self.trashed = message, [], []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, **kwargs):  # noqa: A002, N803
        assert id == self.message["id"]
        return self

    def send(self, userId, body):  # noqa: N803
        self.sent.append(body)
        return self

    def trash(self, userId, id):  # noqa: A002, N803
        self.trashed.append(id)
        return self

    def execute(self):
        return self.message if not self.sent else {"id": "sent-1", "threadId": self.message["threadId"]}


ORIGINAL = {"id": "m1", "threadId": "t1", "payload": {"headers": [
    {"name": "From", "value": "Sara <sara@example.com>"},
    {"name": "Subject", "value": "Dinner tonight"},
    {"name": "Message-ID", "value": "<abc@mail.example.com>"},
    {"name": "References", "value": "<zero@mail.example.com>"},
]}}


@pytest.fixture
def gmail(monkeypatch):
    fake = FakeGmail(dict(ORIGINAL))
    monkeypatch.setattr(google, "service", lambda name, version: fake)
    return fake


def sent_message(fake: FakeGmail):
    return email.message_from_bytes(base64.urlsafe_b64decode(fake.sent[0]["raw"]))


def test_reply_goes_to_the_sender_in_the_same_thread(gmail):
    assert google.email_reply("m1", "I'll be late.") == "reply sent (id sent-1)"
    assert gmail.sent[0]["threadId"] == "t1"
    message = sent_message(gmail)
    assert message["To"] == "Sara <sara@example.com>"
    assert message["Subject"] == "Re: Dinner tonight"
    assert message["In-Reply-To"] == "<abc@mail.example.com>"
    assert message["References"] == "<zero@mail.example.com> <abc@mail.example.com>"
    assert "I'll be late." in message.get_payload()


def test_reply_prefers_reply_to_and_keeps_an_existing_re(gmail):
    gmail.message["payload"] = {"headers": [
        {"name": "From", "value": "news@example.com"}, {"name": "Reply-To", "value": "desk@example.com"},
        {"name": "Subject", "value": "RE: Question"}, {"name": "Message-ID", "value": "<q@x>"}]}
    google.email_reply("m1", "Thanks")
    message = sent_message(gmail)
    assert message["To"] == "desk@example.com" and message["Subject"] == "RE: Question"
    assert message["References"] == "<q@x>"


def test_trash_moves_the_email_to_the_trash(gmail):
    assert google.email_trash("m1") == "moved to the trash"
    assert gmail.trashed == ["m1"]


def test_reply_and_trash_are_confirmed_and_the_draft_is_read_back():
    load_all({"google": {"enabled": True}})
    reply = REGISTRY["email_reply"].action({"id": "m1", "body": "I'll be late.", "to_name": "Sara"})
    assert "Sara" in reply and "I'll be late." in reply
    assert "I'll be late." in REGISTRY["email_send"].action({"to": "a@b.c", "subject": "Hi",
                                                             "body": "I'll be late."})
    trash = REGISTRY["email_trash"].action({"id": "m1", "about": "Sara's email about dinner"})
    assert "trash" in trash and "Sara's email about dinner" in trash
    assert "trash" in REGISTRY["email_trash"].action({"id": "m1"})


def test_a_long_draft_is_shortened_when_read_back():
    load_all({"google": {"enabled": True}})
    action = REGISTRY["email_reply"].action({"id": "m1", "body": "word " * 200})
    assert len(action) < 500 and action.rstrip().endswith("...'")


def test_trash_needs_the_modify_permission():
    assert "https://www.googleapis.com/auth/gmail.modify" in google.SCOPES
