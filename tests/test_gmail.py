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


def test_reply_and_trash_are_confirmed_and_the_draft_is_read_back(gmail):
    load_all({"google": {"enabled": True}})
    reply = REGISTRY["email_reply"].action({"id": "m1", "body": "I'll be late."})
    assert "sara@example.com" in reply and "I'll be late." in reply
    assert "I'll be late." in REGISTRY["email_send"].action({"to": "a@b.c", "subject": "Hi",
                                                             "body": "I'll be late."})
    trash = REGISTRY["email_trash"].action({"id": "m1"})
    assert "trash" in trash and "Sara" in trash and "Dinner tonight" in trash


def test_the_trash_question_names_the_email_from_the_email_itself(gmail):
    """Not "the Medium newsletter" for a bank's sign-in alert because the model said so."""
    load_all({"google": {"enabled": True}})
    gmail.message["payload"] = {"headers": [{"name": "From", "value": "alerts@bank.example"},
                                            {"name": "Subject", "value": "New sign-in"}]}
    trash = REGISTRY["email_trash"].action({"id": "m1", "about": "the Medium newsletter"})
    assert "alerts@bank.example" in trash and "New sign-in" in trash and "Medium" not in trash
    assert "about" not in REGISTRY["email_trash"].params


def test_the_reply_question_names_who_the_email_will_go_to_from_the_email_itself(gmail):
    """The model (or another program) can't make "reply to Sara" go to someone else."""
    load_all({"google": {"enabled": True}})
    gmail.message["payload"] = {"headers": [{"name": "From", "value": "Sara <sara@example.com>"},
                                            {"name": "Reply-To", "value": "drop@evil.example"}]}
    reply = REGISTRY["email_reply"].action({"id": "m1", "body": "Here is the code."})
    assert "drop@evil.example" in reply and "to_name" not in REGISTRY["email_reply"].params


def test_the_reply_is_still_asked_about_when_the_email_can_t_be_read(monkeypatch):
    load_all({"google": {"enabled": True}})
    monkeypatch.setattr(google, "service", lambda name, version: (_ for _ in ()).throw(OSError("offline")))
    assert "that email" in REGISTRY["email_reply"].action({"id": "m1", "body": "Hi"})


def test_a_long_draft_is_shortened_when_read_back(gmail):
    load_all({"google": {"enabled": True}})
    action = REGISTRY["email_reply"].action({"id": "m1", "body": "word " * 200})
    assert len(action) < 500 and action.rstrip().endswith("...'")


def test_trash_needs_the_modify_permission():
    assert "https://www.googleapis.com/auth/gmail.modify" in google.SCOPES


@pytest.mark.parametrize("to", ['"boss@company.com" <x@evil.example>', "Boss <x@evil.example>",
                                "x@evil.example‮", "a@b.example, c@d.example"])
def test_an_email_goes_only_to_one_bare_address(gmail, to):
    """A name in front of the address is what the user would hear, not where the email goes."""
    assert google.email_send(to, "Hi", "the code").startswith("error") and gmail.sent == []


def test_the_reply_question_names_only_the_address_it_goes_to(gmail):
    load_all({"google": {"enabled": True}})
    gmail.message["payload"] = {"headers": [{"name": "From", "value": "Sara <sara@example.com>"},
                                            {"name": "Reply-To", "value": '"sara@example.com" <x@evil.example>'}]}
    reply = REGISTRY["email_reply"].action({"id": "m1", "body": "Here is the code."})
    assert reply.startswith("reply to x@evil.example saying") and "sara@" not in reply


def test_invisible_characters_in_a_sender_don_t_reach_the_question(gmail):
    load_all({"google": {"enabled": True}})
    gmail.message["payload"] = {"headers": [{"name": "From", "value": "alerts@bank‮.example"},
                                            {"name": "Subject", "value": "New​ sign-in"}]}
    trash = REGISTRY["email_trash"].action({"id": "m1"})
    assert "‮" not in trash and "​" not in trash and "New sign-in" in trash
