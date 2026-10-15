"""Configuration parsing and startup failures (contract section 2)."""

import ipaddress
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import APP_DIR, config

SERVER = str(APP_DIR / "server.py")


def run_server(env, user=None):
    """Runs server.py as a subprocess; only used for configurations that exit at startup."""
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
    full_env.update(env)
    return subprocess.run([sys.executable, SERVER], env=full_env, capture_output=True, text=True,
                          timeout=30, user=user)


class StartupExitTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="we-config-")

    def tearDown(self):
        os.chmod(self.data_dir, 0o700)
        os.rmdir(self.data_dir)

    def assertExits(self, env, *expected_in_stderr):
        result = run_server({"DATA_DIR": self.data_dir, **env})
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        for text in expected_in_stderr:
            self.assertIn(text, lines[0])
        self.assertEqual(os.listdir(self.data_dir), [], "no database is created on a config error")

    def test_missing_allowed_hosts(self):
        self.assertExits({}, "WORLD_EATS_ALLOWED_HOSTS must be set, e.g. WORLD_EATS_ALLOWED_HOSTS=localhost")

    def test_empty_allowed_hosts(self):
        self.assertExits({"WORLD_EATS_ALLOWED_HOSTS": " , ,"}, "WORLD_EATS_ALLOWED_HOSTS must be set")

    def test_invalid_allowed_host_entries(self):
        for entry in ("*", "https://x", "a/b", "user@host", "*.example.com"):
            with self.subTest(entry=entry):
                self.assertExits({"WORLD_EATS_ALLOWED_HOSTS": f"localhost,{entry}"},
                                 "WORLD_EATS_ALLOWED_HOSTS", entry)

    def test_invalid_secure_cookies(self):
        for value in ("yes", "1", "", "on"):
            with self.subTest(value=value):
                self.assertExits({"WORLD_EATS_ALLOWED_HOSTS": "localhost",
                                  "WORLD_EATS_SECURE_COOKIES": value}, "WORLD_EATS_SECURE_COOKIES")

    def test_invalid_trusted_proxies(self):
        for value in ("10.0.0.0/8", "proxy.local", "10.0.0.1,999.1.1.1"):
            with self.subTest(value=value):
                self.assertExits({"WORLD_EATS_ALLOWED_HOSTS": "localhost",
                                  "WORLD_EATS_TRUSTED_PROXIES": value}, "WORLD_EATS_TRUSTED_PROXIES")

    def test_data_dir_not_writable(self):
        env = {"WORLD_EATS_ALLOWED_HOSTS": "localhost"}
        if os.getuid() == 0:
            # root can write anywhere, so run the server as nobody against a root-owned 0755 dir
            os.chmod(self.data_dir, 0o755)
            result = run_server({"DATA_DIR": self.data_dir, **env}, user=65534)
            uid = 65534
        else:
            os.chmod(self.data_dir, 0o555)
            result = run_server({"DATA_DIR": self.data_dir, **env})
            uid = os.getuid()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.strip(), f"DATA_DIR {self.data_dir} is not writable by UID {uid}")

    def test_data_dir_missing(self):
        missing = str(Path(self.data_dir, "nope", "deeper"))
        result = run_server({"DATA_DIR": missing, "WORLD_EATS_ALLOWED_HOSTS": "localhost"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"DATA_DIR {missing} is not writable by UID", result.stderr)


class LoadConfigTests(unittest.TestCase):
    def load(self, **env):
        return config.load_config({"WORLD_EATS_ALLOWED_HOSTS": "localhost", **env})

    def test_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.allowed_hosts, (("localhost", None),))
        self.assertFalse(cfg.secure_cookies)
        self.assertEqual(cfg.cookie_name, "we_session")
        self.assertEqual(cfg.trusted_proxies, frozenset())
        self.assertEqual(cfg.data_dir, "/data")
        self.assertEqual(cfg.port, 8080)

    def test_allowed_host_forms(self):
        cfg = self.load(WORLD_EATS_ALLOWED_HOSTS=" LocalHost , ,192.0.2.10:7197,[::1],[::1]:8080,a-b.example.org ")
        self.assertEqual(cfg.allowed_hosts, (
            ("localhost", None), ("192.0.2.10", 7197), ("[::1]", None), ("[::1]", 8080),
            ("a-b.example.org", None)))

    def test_rejected_host_entries(self):
        for entry in ("*", "https://x", "a/b", "user@host", "host:0", "host:65536", "host:", "host:80a",
                      "host:+80", "[::1", "[zz]", "[::1]:", "::1", "a b", "a\tb", "ex_ample", "h\u00e9llo",
                      "x" * 254, "host:80:90"):
            with self.subTest(entry=entry):
                with self.assertRaises(config.ConfigError) as ctx:
                    self.load(WORLD_EATS_ALLOWED_HOSTS=entry)
                self.assertIn("WORLD_EATS_ALLOWED_HOSTS", str(ctx.exception))

    def test_port_boundaries(self):
        self.assertEqual(self.load(WORLD_EATS_ALLOWED_HOSTS="h:1").allowed_hosts, (("h", 1),))
        self.assertEqual(self.load(WORLD_EATS_ALLOWED_HOSTS="h:65535").allowed_hosts, (("h", 65535),))

    def test_secure_cookies(self):
        for value in ("true", "TRUE", "True"):
            cfg = self.load(WORLD_EATS_SECURE_COOKIES=value)
            self.assertTrue(cfg.secure_cookies)
            self.assertEqual(cfg.cookie_name, "__Host-we_session")
        self.assertFalse(self.load(WORLD_EATS_SECURE_COOKIES="FALSE").secure_cookies)

    def test_trusted_proxies(self):
        cfg = self.load(WORLD_EATS_TRUSTED_PROXIES=" 10.0.0.1, ,::1,::ffff:192.0.2.7 ")
        self.assertEqual(cfg.trusted_proxies, frozenset({
            ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("::1"),
            ipaddress.ip_address("192.0.2.7")}))

    def test_split_host_port(self):
        split = config.split_host_port
        self.assertEqual(split("localhost"), ("localhost", None))
        self.assertEqual(split("localhost:80"), ("localhost", "80"))
        self.assertEqual(split("a:b:c"), ("a:b", "c"))
        self.assertEqual(split("[::1]"), ("[::1]", None))
        self.assertEqual(split("[::1]:8080"), ("[::1]", "8080"))
        self.assertEqual(split("[::1]:"), ("[::1]", ""))
        self.assertIsNone(split("[]"))
        self.assertIsNone(split("[::1"))
        self.assertIsNone(split("[::1]x"))

    def test_bad_port(self):
        for value in ("0", "70000", "abc", ""):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                self.load(PORT=value)


if __name__ == "__main__":
    unittest.main()
