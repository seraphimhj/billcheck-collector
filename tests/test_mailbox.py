import unittest

from billcheck_collector.mailbox import ImapMailbox, MailboxError

HDR = b"From: Bank <a@cmbchina.com>\r\n\r\n"
HDR_BAD = b"\r\n"


class FakeConn:
    def __init__(self, typ, data):
        self.typ, self.data = typ, data
        self.calls = []

    def uid(self, *args):
        self.calls.append(args)
        return self.typ, self.data

    def response(self, name):
        return name, self.data


def mb(typ, data):
    m = ImapMailbox.__new__(ImapMailbox)  # skip network login
    m._imap = FakeConn(typ, data)
    return m


class HeadersTest(unittest.TestCase):
    def test_item_order_variants(self):
        shapes = [
            [(b"1 (UID 5 RFC822.SIZE 100 BODY[HEADER.FIELDS (FROM)] {31}", HDR), b")"],
            [(b"1 (UID 5 BODY[HEADER.FIELDS (FROM)] {31}", HDR), b" RFC822.SIZE 100)"],
            [(b"1 (RFC822.SIZE 100 BODY[HEADER.FIELDS (FROM)] {31}", HDR), b" UID 5)"],
        ]
        for data in shapes:
            with self.subTest(data=data):
                self.assertEqual(mb("OK", data).headers([5]), {5: ("a@cmbchina.com", 100)})

    def test_header_fetch_reads_only_from_and_size(self):
        m = mb("OK", [(b"1 (UID 5 RFC822.SIZE 100 BODY[HEADER.FIELDS (FROM)] {31}", HDR), b")"])
        m.headers([5])
        spec = m._imap.calls[0][2]
        self.assertEqual(spec, "(BODY.PEEK[HEADER.FIELDS (FROM)] RFC822.SIZE)")
        self.assertNotIn("MESSAGE-ID", spec)

    def test_unparsable_sender_and_unknown_size(self):
        data = [(b"1 (UID 6 BODY[HEADER.FIELDS (FROM)] {2}", HDR_BAD), b")"]
        self.assertEqual(mb("OK", data).headers([6]), {6: (None, None)})

    def test_no_is_an_error_not_empty(self):
        with self.assertRaises(MailboxError):
            mb("NO", [b"FETCH volume limit exceed"]).headers([1])
        with self.assertRaises(MailboxError):
            mb("NO", [b"x"]).fetch(1)
        with self.assertRaises(MailboxError):
            mb("NO", [b"x"]).search_since(31)

    def test_fetch_ok_without_message_is_lookup_error(self):
        with self.assertRaises(LookupError):
            mb("OK", [None]).fetch(1)

    def test_empty_uidvalidity_raises(self):
        with self.assertRaises(MailboxError):
            mb("OK", [None]).uidvalidity()
        self.assertEqual(mb("OK", [b"77"]).uidvalidity(), "77")
