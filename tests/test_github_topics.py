import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from providers import github_description_language, github_search_query, github_topics


class GitHubTopicsTests(unittest.TestCase):
    def test_creation_date_filters(self):
        with patch("providers.dt.datetime") as clock:
            clock.now.return_value.date.return_value = dt.date(2026, 11, 20)
            query = github_search_query("topic:artificial-intelligence", days=7)
        self.assertIn("created:>=2026-11-14", query)
        self.assertIn("created:2026-01-02..2026-03-04",
                      github_search_query("ai", start="2026-01-02", end="2026-03-04"))
        self.assertIn("created:<=2026-03-04", github_search_query("ai", end="2026-03-04"))
        self.assertNotIn("created:", github_search_query(""))

    def test_invalid_date_ranges(self):
        with self.assertRaisesRegex(ValueError, "有效日期"):
            github_search_query("ai", start="2026-02-30")
        with self.assertRaisesRegex(ValueError, "起始日期"):
            github_search_query("ai", start="2026-03-04", end="2026-01-02")

    def test_languages_of_project_descriptions(self):
        examples = {
            "": "",
            "一个用于构建 AI Agent 的開源工具": "zh",
            "日本語で使えるオープンソースの開発ツール": "ja",
            "AI 개발자를 위한 오픈 소스 도구": "ko",
            "An open source tool for developers to test their applications.": "en",
            "Un logiciel libre pour explorer et analyser les données scientifiques.": "fr",
        }
        for description, language in examples.items():
            with self.subTest(description=description):
                self.assertEqual(github_description_language(description), language)

    @patch("providers.json_request")
    def test_pagination_preserves_descriptions_and_stops_at_github_limit(self, request):
        description = "开源 AI 工具\n支持多种模型。"
        request.return_value = {
            "total_count": 1200, "incomplete_results": False,
            "items": [{"full_name": "example/ai", "html_url": "https://github.com/example/ai",
                       "description": description, "stargazers_count": 1250,
                       "created_at": "2026-11-19T08:00:00Z"}],
        }
        query = github_search_query("AI")
        projects, has_more, partial = github_topics(query, page=2)
        params = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(params, {"q": [query], "sort": ["stars"], "order": ["desc"],
                                  "per_page": ["100"], "page": ["2"]})
        self.assertEqual(projects[0]["notes"], description)
        self.assertEqual(projects[0]["stars"], 1250)
        self.assertEqual(projects[0]["description_language"], "zh")
        self.assertTrue(has_more)
        self.assertFalse(partial)
        self.assertFalse(github_topics(query, page=10)[1])


if __name__ == "__main__":
    unittest.main()
