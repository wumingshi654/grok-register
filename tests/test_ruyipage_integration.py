"""Unit tests for ruyiPage browser framework integration."""
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

import app_config
import browser_runtime
import preflight
import registration_browser


class RuyiPageIntegrationTests(unittest.TestCase):
    def test_config_validation_browser_framework(self):
        cfg = dict(app_config.DEFAULT_CONFIG)
        cfg["browser_framework"] = "ruyipage"
        cfg["browser_path"] = "/custom/path/to/firefox"
        validated = app_config.validate_run_requirements(cfg)
        self.assertEqual(validated["browser_framework"], "ruyipage")
        self.assertEqual(validated["browser_path"], "/custom/path/to/firefox")

        cfg_invalid = dict(app_config.DEFAULT_CONFIG)
        cfg_invalid["browser_framework"] = "selenium"
        with self.assertRaises(app_config.ConfigError):
            app_config.validate_run_requirements(cfg_invalid)

    def test_get_browser_framework_helper(self):
        self.assertEqual(app_config.get_browser_framework({"browser_framework": "ruyipage"}), "ruyipage")
        self.assertEqual(app_config.get_browser_framework({"browser_framework": "firefox"}), "ruyipage")
        self.assertEqual(app_config.get_browser_framework({"browser_framework": "drissionpage"}), "drissionpage")
        self.assertEqual(app_config.get_browser_framework({}), "drissionpage")

    def test_create_ruyipage_options(self):
        with patch.dict(browser_runtime._config, {
            "browser_framework": "ruyipage",
            "browser_path": "",
            "ruyipage_smart_fingerprint": False,
        }):
            opts = browser_runtime.create_browser_options(browser_proxy="http://127.0.0.1:8080", framework="ruyipage")
            from ruyipage import FirefoxOptions
            self.assertIsInstance(opts, FirefoxOptions)
            self.assertEqual(opts._proxy, "http://127.0.0.1:8080")

    def test_dispatch_create_browser_options(self):
        with patch.dict(browser_runtime._config, {"browser_framework": "ruyipage"}):
            opts = browser_runtime.create_browser_options()
            from ruyipage import FirefoxOptions
            self.assertIsInstance(opts, FirefoxOptions)

        with patch.dict(browser_runtime._config, {"browser_framework": "drissionpage"}):
            opts = browser_runtime.create_browser_options()
            from DrissionPage import ChromiumOptions
            self.assertIsInstance(opts, ChromiumOptions)

    def test_page_has_proxy_error_firefox_signatures(self):
        page = Mock()
        page.url = "about:neterror?e=proxyConnectFailure"
        page.run_js = Mock(return_value="The proxy server is refusing connections")
        self.assertTrue(browser_runtime.page_has_proxy_error(page))

    def test_safe_browser_quit(self):
        # Browser with TypeError on del_data
        b1 = Mock()
        def fail_quit(del_data=None):
            if del_data is not None:
                raise TypeError("del_data unexpected")
            return True
        b1.quit.side_effect = fail_quit
        registration_browser._safe_browser_quit(b1)
        self.assertEqual(b1.quit.call_count, 2)

    def test_wait_for_sso_cookie_with_ruyipage_cookie_structure(self):
        mock_page = Mock()
        # Simulate ruyiPage get_cookies returning CookieInfo objects
        class FakeCookie:
            def __init__(self, name, value):
                self.name = name
                self.value = value
        mock_page.get_cookies.return_value = [
            FakeCookie("csrftoken", "abc"),
            FakeCookie("sso", "valid_sso_secret_token_123"),
        ]
        mock_page.run_js.return_value = None

        registration_browser.bind_runtime({
            "sleep_with_cancel": lambda *args: None,
            "raise_if_cancelled": lambda *args: None,
        })

        mock_browser = Mock()
        mock_browser.get_tabs.return_value = [mock_page]

        with patch.object(registration_browser, "page", mock_page), \
             patch.object(registration_browser, "browser", mock_browser), \
             patch.object(registration_browser, "refresh_active_page", return_value=mock_page):
            val = registration_browser.wait_for_sso_cookie(timeout=5)
            self.assertEqual(val, "valid_sso_secret_token_123")

    def test_start_browser_dispatches_ruyipage(self):
        registration_browser.bind_runtime({
            "config": {"browser_framework": "ruyipage"},
            "get_configured_proxy": lambda: "",
            "prepare_browser_proxy": lambda **kwargs: ("", None),
            "create_browser_options": browser_runtime.create_browser_options,
        })
        with patch("ruyipage.Firefox") as mock_firefox:
            mock_browser = Mock()
            mock_tab = Mock()
            mock_browser.get_tabs.return_value = [mock_tab]
            mock_firefox.return_value = mock_browser

            b, p = registration_browser.start_browser(use_proxy=False)
            self.assertEqual(b, mock_browser)
            self.assertEqual(p, mock_tab)
            mock_firefox.assert_called_once()
            registration_browser.stop_browser()

    def test_preflight_checks_with_ruyipage(self):
        cfg = {"browser_framework": "ruyipage"}
        res = preflight._check_browser_runtime(cfg)
        self.assertEqual(res["status"], "OK")
        self.assertIn("ruyiPage", res["message"])


if __name__ == "__main__":
    unittest.main()
