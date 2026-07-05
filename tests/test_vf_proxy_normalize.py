"""spec #2 step 2c — _normalize_proxy_input scheme handling.

A bare host:port[:user:pass] still defaults to socks5 (back-compat); an explicit
http/https/socks5h prefix is honoured so the owner can use an http proxy.
"""

import unittest

from bot.verb_forms_parsing.parser import _normalize_proxy_input as N


class ProxyNormalizeTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(N(""), "")
        self.assertEqual(N("   "), "")

    def test_bare_compact_defaults_socks5(self):
        self.assertEqual(N("gate.example.com:8888:user:pass"), "socks5://user:pass@gate.example.com:8888")

    def test_bare_hostport_defaults_socks5(self):
        self.assertEqual(N("1.2.3.4:1080"), "socks5://1.2.3.4:1080")

    def test_http_prefixed_compact(self):
        self.assertEqual(N("http://gate.example.com:8888:user:pass"), "http://user:pass@gate.example.com:8888")

    def test_socks5h_prefixed_compact(self):
        self.assertEqual(N("socks5h://host:8888:u:p"), "socks5h://u:p@host:8888")

    def test_http_prefixed_hostport(self):
        self.assertEqual(N("http://host:8888"), "http://host:8888")

    def test_full_url_with_userinfo_kept(self):
        self.assertEqual(N("http://user:pass@host:8888"), "http://user:pass@host:8888")
        self.assertEqual(N("socks5://user:pass@host:1080"), "socks5://user:pass@host:1080")

    def test_password_with_colon_preserved(self):
        self.assertEqual(N("http://host:8888:user:pa:ss"), "http://user:pa:ss@host:8888")

    def test_unknown_scheme_kept(self):
        self.assertEqual(N("ftp://whatever"), "ftp://whatever")


if __name__ == "__main__":
    unittest.main()
