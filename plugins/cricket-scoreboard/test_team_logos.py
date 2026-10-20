"""
Team logos: read from ESPN, downloaded, retried, and drawn.

1.3.3 drew a placeholder for every team without a bundled flag:

  * ESPN's cricket feeds give a team's logo as "logo"; only "logos" was read,
    so no team had a logo URL and nothing was ever downloaded.
  * A failed download saved a placeholder under the logo's own name, which the
    "already have it" check then took for the logo: never fetched again.
  * The renderer cached "no crest", so a logo downloaded after a team's first
    frame was not drawn until a restart.
  * National teams never downloaded a logo, so a side without one of the
    thirteen bundled flags only ever had a placeholder.

Run:  python -m unittest test_team_logos  (needs Pillow + requests)
"""

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import cricket_logo_downloader  # noqa: E402
import cricket_renderer  # noqa: E402
from cricket_data_fetcher import CricketDataFetcher  # noqa: E402
from cricket_logo_downloader import create_placeholder_logo, is_placeholder_logo  # noqa: E402
from test_cricket_plugin import (_load_harness_config, _load_mock_matches,  # noqa: E402
                                 _MockCacheManager, _MockDisplayManager)

QLD_URL = "https://a.espncdn.com/i/teamlogos/cricket/500/459.png"


def _png(colour=(128, 0, 32, 255), size=(500, 500)):
    buf = io.BytesIO()
    Image.new("RGBA", size, colour).save(buf, "PNG")
    return buf.getvalue()


class _Response:
    def __init__(self, body, status=200, content_type="image/png"):
        self.body, self.status_code = body, status
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]


class _Session:
    def __init__(self):
        self.calls, self.reply = [], (lambda url: _Response(_png()))

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.reply(url)


def _team(abbr="QLD", name="Queensland", url=QLD_URL):
    return {"abbr": abbr, "name": name, "id": "459", "logo_url": url}


class TestLogoUrl(unittest.TestCase):
    def test_reads_espns_logo_field(self):
        fetcher = object.__new__(CricketDataFetcher)
        team = fetcher._parse_competitor({"team": {
            "id": "459", "abbreviation": "QLD", "displayName": "Queensland",
            "logo": QLD_URL}})
        self.assertEqual(team["logo_url"], QLD_URL)

    def test_falls_back_to_logos_then_none(self):
        fetcher = object.__new__(CricketDataFetcher)
        listed = fetcher._parse_competitor({"team": {"logos": [{"href": QLD_URL}]}})
        self.assertEqual(listed["logo_url"], QLD_URL)
        self.assertIsNone(fetcher._parse_competitor({"team": {}})["logo_url"])


class _Sandbox(unittest.TestCase):
    """The plugin with its assets in a temp dir and a fake download session."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "assets").mkdir()
        shutil.copytree(_HERE / "assets" / "flags", tmp / "assets" / "flags")
        self.logos = tmp / "assets" / "logos"
        self.logos.mkdir()
        for patcher in (mock.patch.object(cricket_renderer, "_PLUGIN_DIR", tmp),
                        mock.patch.object(cricket_logo_downloader._downloader, "session",
                                          _Session())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = cricket_logo_downloader._downloader.session


class TestDownload(_Sandbox):
    def test_saves_a_decodable_png(self):
        path = self.logos / "QLD.png"
        self.assertTrue(cricket_logo_downloader.download_missing_logo(
            "cricket", "459", "QLD", path, QLD_URL))
        with Image.open(path) as img:
            self.assertEqual((img.format, img.mode, img.size), ("PNG", "RGBA", (500, 500)))
        self.assertEqual(list(self.logos.iterdir()), [path], "no temp file left behind")

    def test_a_failure_writes_nothing(self):
        path = self.logos / "QLD.png"
        for reply in (lambda u: _Response(b"<html>not found</html>", 200, "text/html"),
                      lambda u: _Response(b"", 404),
                      lambda u: _Response(b"not really a png", 200, "image/png")):
            self.session.reply = reply
            self.assertFalse(cricket_logo_downloader.download_missing_logo(
                "cricket", "459", "QLD", path, QLD_URL))
            self.assertEqual(list(self.logos.iterdir()), [], "no file, no placeholder")

    def test_old_placeholders_are_recognised(self):
        old = self.logos / "QLD.png"
        create_placeholder_logo("QLD", old)
        self.assertTrue(is_placeholder_logo(old))
        real = self.logos / "VIC.png"
        Image.new("RGBA", (500, 500), (0, 40, 120, 255)).save(real)
        self.assertFalse(is_placeholder_logo(real))


class TestPlugin(_Sandbox):
    def setUp(self):
        super().setUp()
        from manager import CricketScoreboardPlugin
        self.plugin = CricketScoreboardPlugin("cricket-scoreboard", _load_harness_config(),
                                              _MockDisplayManager(), _MockCacheManager(), None)
        self.plugin.logos_dir = self.logos
        self.clock = 1_000_000.0
        patcher = mock.patch("manager.time.time", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def match(self, *teams, key="sheffield-shield"):
        return {"competition_key": key, "teams": list(teams)}

    def test_domestic_team_logo_is_downloaded_and_drawn(self):
        team = _team()
        renderer = self.plugin.renderer
        self.assertIsNone(renderer._load_crest(team, 16), "nothing yet")
        self.plugin._ensure_logos(self.match(team))
        self.assertEqual(self.session.calls, [QLD_URL])
        crest = renderer._load_crest(team, 16)
        self.assertIsNotNone(crest, "drawn straight after the download, no restart")
        self.assertEqual(crest.size, (16, 16))

    def test_national_team_without_a_flag_gets_its_logo(self):
        team = _team("NED", "Netherlands", "https://a.espncdn.com/i/teamlogos/cricket/500/15.png")
        self.plugin._ensure_logos(self.match(team, key="international"))
        self.assertTrue((self.logos / "NED.png").exists())

    def test_bundled_flags_are_not_downloaded(self):
        team = _team("AUS", "Australia", "https://a.espncdn.com/i/teamlogos/cricket/500/2.png")
        self.assertTrue(self.plugin.renderer.has_bundled_flag(team))
        self.plugin._ensure_logos(self.match(team, key="international"))
        self.assertEqual(self.session.calls, [])

    def test_an_old_placeholder_is_replaced(self):
        create_placeholder_logo("QLD", self.logos / "QLD.png")
        self.plugin._ensure_logos(self.match(_team()))
        self.assertEqual(self.session.calls, [QLD_URL])
        self.assertFalse(is_placeholder_logo(self.logos / "QLD.png"))

    def test_a_failed_download_is_retried_later_not_every_update(self):
        self.session.reply = lambda u: _Response(b"", 503)
        team = _team()
        self.plugin._ensure_logos(self.match(team))
        self.plugin._ensure_logos(self.match(team))
        self.assertEqual(len(self.session.calls), 1, "backs off")
        self.session.reply = lambda u: _Response(_png())
        self.clock += 31 * 60
        self.plugin._ensure_logos(self.match(team))
        self.assertEqual(len(self.session.calls), 2)
        self.assertTrue((self.logos / "QLD.png").exists())

    def test_an_existing_logo_is_not_fetched_again(self):
        Image.new("RGBA", (500, 500), (128, 0, 32, 255)).save(self.logos / "QLD.png")
        self.plugin._ensure_logos(self.match(_team()))
        self.assertEqual(self.session.calls, [])

    def test_harness_matches_still_render(self):
        for m in _load_mock_matches():
            self.plugin._ensure_logos(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
