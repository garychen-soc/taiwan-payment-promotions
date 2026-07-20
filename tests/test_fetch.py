from __future__ import annotations

import ssl
import subprocess
import unittest
import urllib.request
from email.message import Message
from unittest.mock import Mock, patch

from payment_promotions_monitor.fetch import (
    _AllowListRedirectHandler,
    _fetch_with_system_curl,
    _open_with_safe_redirects,
    is_allowed_url,
)


MARKER = b"\n__PAYMENT_MONITOR_META__"


def curl_response(
    status: int,
    effective_url: str,
    *,
    redirect_url: str = "",
    content_type: str = "text/html; charset=utf-8",
    body: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    metadata = f"{status}\t{effective_url}\t{content_type}\t{redirect_url}".encode()
    return subprocess.CompletedProcess([], 0, stdout=body + MARKER + metadata, stderr=b"")


class AllowListRedirectHandlerTests(unittest.TestCase):
    def test_rejects_external_redirect_before_parent_can_create_request(self) -> None:
        handler = _AllowListRedirectHandler(["official.example"])
        request = urllib.request.Request("https://official.example/start")

        with patch.object(urllib.request.HTTPRedirectHandler, "redirect_request") as parent_redirect:
            with self.assertRaisesRegex(ValueError, "outside official domain"):
                handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    Message(),
                    "https://attacker.example/collect",
                )

        parent_redirect.assert_not_called()

    def test_allows_relative_redirect_and_validates_absolute_target_first(self) -> None:
        handler = _AllowListRedirectHandler(["official.example"])
        request = urllib.request.Request("https://official.example/path/start")
        sentinel = object()

        with patch.object(
            urllib.request.HTTPRedirectHandler,
            "redirect_request",
            return_value=sentinel,
        ) as parent_redirect:
            result = handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "../next",
            )

        self.assertIs(result, sentinel)
        self.assertEqual(parent_redirect.call_args.args[-1], "https://official.example/next")

    def test_safe_opener_installs_allow_list_handler(self) -> None:
        opener = Mock()
        response = object()
        opener.open.return_value = response
        request = urllib.request.Request("https://official.example/start")

        with patch("urllib.request.build_opener", return_value=opener) as build_opener:
            result = _open_with_safe_redirects(
                request,
                ["official.example"],
                timeout=5,
                context=ssl.create_default_context(),
            )

        self.assertIs(result, response)
        self.assertTrue(any(isinstance(item, _AllowListRedirectHandler) for item in build_opener.call_args.args))
        opener.open.assert_called_once_with(request, timeout=5)


class SystemCurlRedirectTests(unittest.TestCase):
    def test_rejects_external_redirect_without_requesting_target(self) -> None:
        first = curl_response(
            302,
            "https://official.example/start",
            redirect_url="https://attacker.example/collect",
        )

        with patch("subprocess.run", return_value=first) as run:
            with self.assertRaisesRegex(ValueError, "outside official domain"):
                _fetch_with_system_curl(
                    "https://official.example/start",
                    ["official.example"],
                    timeout=5,
                    max_bytes=1024,
                )

        run.assert_called_once()
        self.assertNotIn("--location", run.call_args.args[0])

    def test_follows_allowed_redirect_one_checked_request_at_a_time(self) -> None:
        first = curl_response(
            301,
            "https://official.example/start",
            redirect_url="/offers/current",
        )
        second = curl_response(
            200,
            "https://official.example/offers/current",
            content_type="application/json; charset=utf-8",
            body=b'{"ok":true}',
        )

        with patch("subprocess.run", side_effect=[first, second]) as run:
            result = _fetch_with_system_curl(
                "https://official.example/start",
                ["official.example"],
                timeout=5,
                max_bytes=1024,
            )

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], "https://official.example/start")
        self.assertEqual(run.call_args_list[1].args[0][-1], "https://official.example/offers/current")
        self.assertTrue(all("--location" not in call.args[0] for call in run.call_args_list))
        self.assertEqual(result.final_url, "https://official.example/offers/current")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "application/json")
        self.assertEqual(result.body, b'{"ok":true}')


class AllowedUrlTests(unittest.TestCase):
    def test_domains_are_case_and_trailing_dot_insensitive(self) -> None:
        self.assertTrue(is_allowed_url("https://News.Official.Example./item", ["OFFICIAL.EXAMPLE."]))

    def test_similar_but_unrelated_host_is_rejected(self) -> None:
        self.assertFalse(is_allowed_url("https://official.example.attacker.test/item", ["official.example"]))


if __name__ == "__main__":
    unittest.main()
