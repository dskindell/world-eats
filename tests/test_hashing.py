"""Password hashing with the real parameters (contract section 4)."""

import base64
import re
import threading
import unittest

from tests.helpers import auth


class HashingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hasher = auth.PasswordHasher()
        cls.stored = cls.hasher.hash("correct horse battery \u00e9\U0001F600")

    def test_real_parameters(self):
        h = auth.PasswordHasher()
        self.assertEqual((h.n, h.r, h.p, h.maxmem), (32768, 8, 3, 67108864))
        self.assertIsInstance(h.semaphore, type(threading.BoundedSemaphore()))
        self.assertEqual(h.timeout, 10)

    def test_stored_format(self):
        self.assertRegex(self.stored, r"^scrypt\$32768\$8\$3\$[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*$")
        parts = self.stored.split("$")
        self.assertEqual(len(base64.b64decode(parts[4], validate=True)), 16)
        self.assertEqual(len(base64.b64decode(parts[5], validate=True)), 32)
        self.assertEqual(auth.parse_hash(self.stored)[:3], (32768, 8, 3))

    def test_round_trip(self):
        self.assertTrue(self.hasher.verify("correct horse battery \u00e9\U0001F600", self.stored))
        self.assertFalse(self.hasher.verify("correct horse battery \u00e9", self.stored))
        self.assertFalse(self.hasher.verify("Correct horse battery \u00e9\U0001F600", self.stored))

    def test_salted(self):
        self.assertNotEqual(self.hasher.hash("same password!"), self.hasher.hash("same password!"))

    def test_dummy_hash(self):
        hasher = auth.PasswordHasher(n=1024, p=1)
        hasher.init_dummy()
        self.assertTrue(hasher.dummy_hash.startswith("scrypt$1024$8$1$"))
        self.assertFalse(hasher.burn("anything at all"))
        self.assertFalse(hasher.burn(None))
        self.assertFalse(hasher.burn("\ud800"))

    def test_burn_needs_dummy(self):
        with self.assertRaises(RuntimeError):
            auth.PasswordHasher(n=1024, p=1).burn("anything")

    def test_bad_stored_values(self):
        for stored in ("", "plain", "scrypt$1$2$3", "scrypt$x$8$1$AAAA$AAAA", "bcrypt$1024$8$1$AAAA$AAAA",
                       "scrypt$1024$8$1$!!!!$AAAA", None):
            with self.subTest(stored=stored):
                self.assertIsNone(auth.parse_hash(stored))
                self.assertFalse(self.hasher.verify("whatever", stored))

    def test_semaphore_timeout(self):
        hasher = auth.PasswordHasher(n=1024, p=1, timeout=0.1)
        hasher.semaphore.acquire()
        hasher.semaphore.acquire()
        try:
            with self.assertRaises(auth.Busy):
                hasher.hash("password password")
        finally:
            hasher.semaphore.release()
            hasher.semaphore.release()


class ValidationUnitTests(unittest.TestCase):
    def test_username(self):
        for ok in ("abc", "a" * 32, "A.b_c-9", "..."):
            self.assertTrue(auth.valid_username(ok), ok)
        for bad in ("ab", "a" * 33, "a b", "\u00e4bc", "abc\n", "ab\u0663", 123, None, b"abc"):
            self.assertFalse(auth.valid_username(bad), bad)

    def test_validators_raise_invalid(self):
        self.assertEqual(auth.require_username("abc"), "abc")
        self.assertEqual(auth.require_new_password("x" * 12), "x" * 12)
        self.assertEqual(auth.clean_display_name(" A "), "A")
        for func, value, message in (
                (auth.require_username, "ab", auth.USERNAME_MSG),
                (auth.require_new_password, "short", auth.PASSWORD_MSG),
                (auth.clean_display_name, "", auth.DISPLAY_NAME_LENGTH_MSG),
                (auth.clean_display_name, "a\u200bb", auth.DISPLAY_NAME_CHARS_MSG)):
            with self.subTest(func=func.__name__, value=value):
                with self.assertRaises(auth.Invalid) as ctx:
                    func(value)
                self.assertEqual(str(ctx.exception), message)

    def test_zero_width_joiner_constant(self):
        self.assertEqual(auth.ZERO_WIDTH_JOINER, "\u200d")

    def test_passwords(self):
        self.assertTrue(auth.valid_new_password(" " * 12))
        self.assertTrue(auth.valid_new_password("\U0001F600" * 12))  # code points, not bytes
        self.assertFalse(auth.valid_new_password("\U0001F600" * 11))
        self.assertTrue(auth.valid_new_password("x" * 128))
        self.assertFalse(auth.valid_new_password("x" * 129))
        self.assertTrue(auth.usable_password("x"))
        self.assertFalse(auth.usable_password(""))
        self.assertFalse(auth.usable_password("x" * 129))
        self.assertFalse(auth.usable_password("abc\udc80"))


if __name__ == "__main__":
    unittest.main()
