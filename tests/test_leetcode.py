import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from leetcode_table_bot.config import BotError
from leetcode_table_bot.leetcode import LeetCodeClient, Problem

USERNAME = "1-andro1d-1"


def task(number, slug, **extra):
    return {"number": str(number), "title": slug.replace("-", " "), "titleSlug": slug, "difficulty": "Easy", "status": "ac", **extra}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = LeetCodeClient(USERNAME, Path(self.directory.name))

    def test_public_duplicates_do_not_hide_incomplete_history(self):
        data = {"matchedUser": {"submitStatsGlobal": {"acSubmissionNum": [{"difficulty": "All", "count": 3}]}},
                "recentAcSubmissionList": [{"title": "one", "titleSlug": "one", "timestamp": "1"},
                                           {"title": "one", "titleSlug": "one", "timestamp": "2"},
                                           {"title": "two", "titleSlug": "two", "timestamp": "3"}]}
        with patch.object(self.client, "_query", return_value=data):
            snapshot = self.client.fetch_snapshot()
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.total_solved, 3)
        self.assertEqual([problem.slug for problem in snapshot.problems], ["one", "two"])
        self.assertEqual(snapshot.problems[0].timestamp, 2)

    def test_empty_profile_is_valid(self):
        data = {"matchedUser": {"submitStatsGlobal": {"acSubmissionNum": [{"difficulty": "All", "count": 0}]}}, "recentAcSubmissionList": []}
        with patch.object(self.client, "_query", return_value=data):
            self.assertTrue(self.client.fetch_snapshot().complete)

    def test_missing_profile_is_not_treated_as_no_solved_tasks(self):
        with patch.object(self.client, "_query", return_value={"matchedUser": None}):
            with self.assertRaisesRegex(BotError, "не найден"):
                self.client.fetch_snapshot()

    def test_full_history_paginates_only_accepted_tasks(self):
        self.client.authenticated = True
        responses = [
            {"userStatus": {"isSignedIn": True, "username": USERNAME}},
            {"solved": {"total": 3, "questions": [task(1, "one"), task(2, "two")]}},
            {"solved": {"total": 3, "questions": [task(3, "three")]}},
        ]
        with patch.object(self.client, "_query", side_effect=responses) as query:
            snapshot = self.client.fetch_snapshot()
        self.assertTrue(snapshot.complete)
        self.assertEqual([problem.number for problem in snapshot.problems], [1, 2, 3])
        self.assertEqual(query.call_args_list[-1].args[1]["skip"], 2)

    def test_session_for_another_user_is_rejected_before_private_history(self):
        with patch.object(self.client, "_query", return_value={"userStatus": {"isSignedIn": True, "username": "another"}}) as query:
            with self.assertRaisesRegex(BotError, "другой аккаунт"):
                self.client.login("private-session", "private-csrf")
        query.assert_called_once()
        self.assertFalse(self.client.session_file.exists())

    def test_verified_login_stays_local_and_is_private(self):
        with patch.object(self.client, "_query", return_value={"userStatus": {"isSignedIn": True, "username": USERNAME}}):
            self.client.login("private-session", "private-csrf")
        self.assertEqual(self.client.session_file.stat().st_mode & 0o777, 0o600)
        restored = LeetCodeClient(USERNAME, Path(self.directory.name))
        self.assertTrue(restored.authenticated)
        self.assertFalse(restored.verified)

    def test_expired_session_is_rejected(self):
        with patch.object(self.client, "_query", return_value={"userStatus": {"isSignedIn": False}}):
            with self.assertRaisesRegex(BotError, "истекла"):
                self.client.login("private-session", "private-csrf")

    def test_full_history_rejects_unaccepted_results(self):
        self.client.authenticated = self.client.verified = True
        with patch.object(self.client, "_query", side_effect=[{"userStatus": {"isSignedIn": True, "username": USERNAME}}, {"solved": {"total": 1, "questions": [task(1, "one", status=None)]}}]):
            with self.assertRaisesRegex(BotError, "Accepted"):
                self.client.fetch_snapshot()

    def test_full_history_does_not_claim_success_on_missing_page(self):
        self.client.authenticated = self.client.verified = True
        with patch.object(self.client, "_query", side_effect=[{"userStatus": {"isSignedIn": True, "username": USERNAME}}, {"solved": {"total": 2, "questions": [task(1, "one")]}}, {"solved": {"total": 2, "questions": []}}]):
            with self.assertRaisesRegex(BotError, "неполный"):
                self.client.fetch_snapshot()

    def test_session_expiring_between_polls_is_not_empty_success(self):
        self.client.authenticated = self.client.verified = True
        with patch.object(self.client, "_query", return_value={"userStatus": {"isSignedIn": False}}):
            with self.assertRaisesRegex(BotError, "истекла"):
                self.client.fetch_snapshot()

    def test_question_number_resolution_is_cached(self):
        candidate = Problem(None, "Two Sum", "two-sum", timestamp=20)
        with patch.object(self.client, "_query", return_value={"question": task(1, "two-sum")}) as query:
            result = self.client.resolve(candidate)
            self.assertEqual(self.client.resolve(candidate), result)
        self.assertEqual(result.number, 1)
        self.assertEqual(result.timestamp, 20)
        query.assert_called_once()

    def test_query_keeps_cookies_on_leetcode_and_blocks_redirects(self):
        response = Mock(is_redirect=False)
        response.json.return_value = {"data": {"ok": True}}
        with patch.object(self.client.http, "post", return_value=response) as post:
            self.assertEqual(self.client._query("query {}"), {"ok": True})
        self.assertEqual(post.call_args.args[0], "https://leetcode.com/graphql/")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_403_is_reported_without_private_tokens(self):
        self.client._set_cookies("private-session", "private-csrf")
        response = requests.Response()
        response.status_code = 403
        with patch.object(self.client.http, "post", side_effect=requests.HTTPError(response=response)):
            with self.assertRaisesRegex(BotError, "403") as error:
                self.client._query("query {}")
        self.assertNotIn("private-session", str(error.exception))
