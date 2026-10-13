import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from greenline_bot.config import ConfigError, load_config


class Config(unittest.TestCase):
    def _load(self, text: str, **kw):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_bytes(text.encode("utf-8-sig"))      # как сохраняет Блокнот Windows
            with mock.patch.dict(os.environ, {}, clear=True):
                return load_config(p, **kw)

    def test_notepad_bom_and_example_defaults(self):
        example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
        cfg = self._load(example.replace("BOT_TOKEN=", "BOT_TOKEN=123:abc", 1))
        self.assertEqual((cfg.bot_token, cfg.storage), ("123:abc", "memory"))

    def test_empty_token_is_clear_error(self):
        with self.assertRaises(ConfigError) as e:
            self._load("BOT_TOKEN=\nSTORAGE=memory\n")
        self.assertIn("BOT_TOKEN", str(e.exception))

    def test_sheets_needs_table(self):
        with self.assertRaises(ConfigError):
            self._load("BOT_TOKEN=1:a\nSTORAGE=sheets\n")


if __name__ == "__main__":
    unittest.main()
