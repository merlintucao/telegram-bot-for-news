from __future__ import annotations

import unittest

from news_bot.html_text import html_to_text


class HTMLTextTests(unittest.TestCase):
    def test_html_to_text_preserves_breaks_and_links(self) -> None:
        html = "<p>Hello<br>world</p><p><a href='https://example.com'>Read more</a></p>"
        self.assertEqual(
            html_to_text(html),
            "Hello\nworld\n\nRead more (https://example.com)",
        )


if __name__ == "__main__":
    unittest.main()

