"""Unit tests for Outlook external API integration (accounts & temp modes,

concurrency reservation, email polling, status update, and group migration).
"""
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import app_config
from outlook_api import (
    OutlookApiAccount,
    OutlookApiClient,
    OutlookApiAccountPool,
    OutlookApiTaskRuntime,
    create_outlook_api_task_runtime,
    probe_outlook_api,
)
from outlook_mailbox_pool import create_outlook_task_runtime, is_outlook_handle
from registration_flow import VerificationCodeUnavailable


class OutlookApiClientTests(unittest.TestCase):
    def setUp(self):
        self.api_base = "http://127.0.0.1:5000"
        self.api_key = "test-api-key"
        self.web_password = "test-web-password"
        self.client = OutlookApiClient(
            api_base=self.api_base,
            api_key=self.api_key,
            web_password=self.web_password,
        )

    @patch("requests.Session.get")
    def test_get_accounts_success(self, mock_get):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "accounts": [
                {"id": 1, "email": "user1@outlook.com", "status": "active", "group_id": 10},
                {"id": 2, "email": "user2@outlook.com", "status": "inactive", "group_id": 20},
            ],
        }
        mock_get.return_value = mock_resp

        accounts = self.client.get_accounts(group_id=10)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0].email, "user1@outlook.com")
        self.assertEqual(accounts[0].id, 1)

        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "http://127.0.0.1:5000/api/external/accounts")
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-api-key")
        self.assertEqual(kwargs["params"]["group_id"], "10")

    @patch("requests.Session.get")
    def test_get_emails_success(self, mock_get):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "emails": [
                {
                    "subject": "Your verification code",
                    "text": "Your Grok security code is: 849201",
                    "sender": "noreply@x.ai",
                }
            ],
        }
        mock_get.return_value = mock_resp

        emails = self.client.get_emails("user1@outlook.com", folder="inbox", top=5)
        self.assertEqual(len(emails), 1)
        self.assertIn("849201", emails[0]["text"])

        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "http://127.0.0.1:5000/api/external/emails")
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-api-key")
        self.assertEqual(kwargs["params"]["email"], "user1@outlook.com")
        self.assertEqual(kwargs["params"]["folder"], "inbox")
        self.assertEqual(kwargs["params"]["top"], 5)

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_extension_login_and_csrf_flow(self, mock_post, mock_get):
        # 1. POST /api/extension/login returns launch_url
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.headers = {"Content-Type": "application/json"}
        login_resp.json.return_value = {
            "success": True,
            "launch_url": "/extension-login/token123?next=/",
        }
        mock_post.return_value = login_resp

        # 2. GET /extension-login/... and GET /api/csrf-token
        exchange_resp = Mock()
        exchange_resp.status_code = 200
        csrf_resp = Mock()
        csrf_resp.status_code = 200
        csrf_resp.json.return_value = {"csrf_token": "csrf-secret-999"}
        mock_get.side_effect = [exchange_resp, csrf_resp]

        success = self.client.login()
        self.assertTrue(success)
        self.assertEqual(self.client._csrf_token, "csrf-secret-999")

        mock_post.assert_called_once_with(
            "http://127.0.0.1:5000/api/extension/login",
            json={"password": "test-web-password"},
            timeout=15.0,
        )

    @patch("requests.Session.request")
    def test_update_account_status(self, mock_request):
        self.client._logged_in = True
        self.client._csrf_token = "csrf-secret-999"

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_request.return_value = mock_resp

        ok = self.client.update_account_status(123, "inactive")
        self.assertTrue(ok)

        mock_request.assert_called_once()
        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(args[1], "http://127.0.0.1:5000/api/accounts/123")
        self.assertEqual(kwargs["headers"]["X-CSRFToken"], "csrf-secret-999")
        self.assertEqual(kwargs["json"], {"status": "inactive"})

    @patch("requests.Session.request")
    def test_batch_update_group(self, mock_request):
        self.client._logged_in = True
        self.client._csrf_token = "csrf-secret-999"

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_request.return_value = mock_resp

        ok = self.client.batch_update_group([123, 456], "5")
        self.assertTrue(ok)

        mock_request.assert_called_once()
        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "http://127.0.0.1:5000/api/accounts/batch-update-group")
        self.assertEqual(kwargs["headers"]["X-CSRFToken"], "csrf-secret-999")
        self.assertEqual(kwargs["json"], {"account_ids": [123, 456], "group_id": 5})

    @patch("requests.Session.get")
    def test_temp_emails_and_messages(self, mock_get):
        temp_list_resp = Mock()
        temp_list_resp.status_code = 200
        temp_list_resp.json.return_value = {"temp_emails": ["temp1@example.com", "temp2@example.com"]}

        messages_resp = Mock()
        messages_resp.status_code = 200
        messages_resp.json.return_value = {
            "messages": [
                {"subject": "Verification", "text": "Your confirmation code is 123456", "sender": "noreply@x.ai"}
            ]
        }
        mock_get.side_effect = [temp_list_resp, messages_resp]

        temps = self.client.get_temp_emails()
        self.assertEqual(temps, ["temp1@example.com", "temp2@example.com"])

        msgs = self.client.get_temp_email_messages("temp1@example.com")
        self.assertEqual(len(msgs), 1)
        self.assertIn("123456", msgs[0]["text"])


class OutlookApiAccountPoolTests(unittest.TestCase):
    def test_concurrency_reservation_and_round_robin(self):
        accounts = [
            OutlookApiAccount(email="a@outlook.com", id=1),
            OutlookApiAccount(email="b@outlook.com", id=2),
            OutlookApiAccount(email="c@outlook.com", id=3),
        ]
        pool = OutlookApiAccountPool(accounts, strategy="round_robin")
        self.assertEqual(pool.count, 3)
        self.assertEqual(pool.remaining, 3)

        # Acquire 1
        acc1 = pool.acquire()
        self.assertEqual(acc1.email, "a@outlook.com")

        # Acquire 2
        acc2 = pool.acquire()
        self.assertEqual(acc2.email, "b@outlook.com")

        # Acquire 3
        acc3 = pool.acquire()
        self.assertEqual(acc3.email, "c@outlook.com")

        # Now all are in use, acquire should fail
        with self.assertRaises(RuntimeError) as cm:
            pool.acquire()
        self.assertIn("预留", str(cm.exception))

        # Releasing acc2 before submit allows it to be acquired again
        pool.release_unsubmitted(acc2.email)
        acc_reacquired = pool.acquire()
        self.assertEqual(acc_reacquired.email, "b@outlook.com")

        # Mark acc1 completed
        pool.mark_completed(acc1.email)
        self.assertEqual(pool.remaining, 2)


class OutlookApiTaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.mock_client = Mock(spec=OutlookApiClient)
        self.mock_client.api_base = "http://127.0.0.1:5000"
        self.mock_client.api_key = "key"
        self.mock_client.web_password = "pwd"
        self.mock_client.get_accounts.return_value = [
            OutlookApiAccount(email="acc1@outlook.com", id=101, status="active"),
            OutlookApiAccount(email="acc2@outlook.com", id=102, status="active"),
        ]

    def test_runtime_acquire_and_wait_for_code_success(self):
        runtime = OutlookApiTaskRuntime(
            api_client=self.mock_client,
            mode="accounts",
            strategy="round_robin",
            auto_disable_on_success=True,
            failed_group_id="99",
        )
        self.assertEqual(runtime.count, 2)

        email, handle = runtime.acquire()
        self.assertEqual(email, "acc1@outlook.com")
        self.assertTrue(is_outlook_handle(handle))

        # Mock email messages returned by API
        self.mock_client.get_emails.return_value = [
            {
                "subject": "Sign-up code",
                "text": "Your confirmation code is: 582103",
                "sender": "noreply@x.ai",
            }
        ]

        code = runtime.wait_for_code(handle, email, timeout=10, poll_interval=1)
        self.assertEqual(code, "582103")

        # Succeeded: on_success triggers update_account_status to inactive
        runtime.on_success(email)
        self.mock_client.update_account_status.assert_called_once_with(101, "inactive")

    def test_runtime_wait_for_code_timeout_moves_to_failed_group(self):
        runtime = OutlookApiTaskRuntime(
            api_client=self.mock_client,
            mode="accounts",
            failed_group_id="99",
            move_group_on_failure=True,
        )
        email, handle = runtime.acquire()

        # Email polling returns no code
        self.mock_client.get_emails.return_value = []

        with self.assertRaises(VerificationCodeUnavailable):
            runtime.wait_for_code(handle, email, timeout=1, poll_interval=1)

        # Failure: on_failure was triggered and moved to group 99
        self.mock_client.batch_update_group.assert_called_once_with([101], "99")

    def test_runtime_temp_mode(self):
        self.mock_client.get_temp_emails.return_value = ["temp-user@example.com"]
        runtime = OutlookApiTaskRuntime(
            api_client=self.mock_client,
            mode="temp",
        )
        self.assertEqual(runtime.count, 1)

        email, handle = runtime.acquire()
        self.assertEqual(email, "temp-user@example.com")

        self.mock_client.get_temp_email_messages.return_value = [
            {"subject": "Code", "text": "Your code is 654321", "sender": "noreply@grok.com"}
        ]
        code = runtime.wait_for_code(handle, email, timeout=5, poll_interval=1)
        self.assertEqual(code, "654321")


class OutlookApiConfigIntegrationTests(unittest.TestCase):
    def test_config_validation_api_mode(self):
        cfg = dict(app_config.DEFAULT_CONFIG)
        cfg["email_provider"] = "outlook"
        cfg["outlook_source"] = "api"
        cfg["outlook_api_base"] = "http://127.0.0.1:5000"
        cfg["outlook_api_key"] = "test-key"
        cfg["outlook_web_password"] = "secret"
        cfg["outlook_failed_group_id"] = "5"

        validated = app_config.validate_run_requirements(cfg)
        self.assertEqual(validated["outlook_source"], "api")
        self.assertEqual(validated["outlook_api_base"], "http://127.0.0.1:5000")

    def test_create_outlook_task_runtime_dispatches_api_mode(self):
        cfg = dict(app_config.DEFAULT_CONFIG)
        cfg["email_provider"] = "outlook"
        cfg["outlook_source"] = "api"
        cfg["outlook_api_base"] = "http://127.0.0.1:5000"
        cfg["outlook_api_key"] = "test-key"

        with patch("outlook_api.OutlookApiClient.get_accounts") as mock_get_acc:
            mock_get_acc.return_value = [OutlookApiAccount(email="test@outlook.com", id=1)]
            runtime = create_outlook_task_runtime(cfg)
            self.assertIsInstance(runtime, OutlookApiTaskRuntime)
            self.assertEqual(runtime.count, 1)


if __name__ == "__main__":
    unittest.main()
