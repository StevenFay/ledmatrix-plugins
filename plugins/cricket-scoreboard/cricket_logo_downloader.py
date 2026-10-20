"""
Simplified LogoDownloader for plugin use
"""

import logging
import requests
from typing import List, Optional
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

class LogoDownloader:
    """Simplified logo downloader for team logos from ESPN API."""

    def __init__(self, request_timeout: int = 30, retry_attempts: int = 3):
        """Initialize the logo downloader with HTTP session and retry logic."""
        self.request_timeout = request_timeout
        self.retry_attempts = retry_attempts

        # Set up session with retry logic
        self.session = requests.Session()
        retry_strategy = Retry(
            total=retry_attempts,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)

        # Set up headers
        self.headers = {
            'User-Agent': 'LEDMatrix/2.0 (https://github.com/ChuckBuilds/LEDMatrix)',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive'
        }

    @staticmethod
    def normalize_abbreviation(abbr: str) -> str:
        """Normalize team abbreviation for filename."""
        return abbr.upper()

    @staticmethod
    def get_logo_filename_variations(abbr: str) -> List[str]:
        """Get possible filename variations for a team abbreviation."""
        normalized = LogoDownloader.normalize_abbreviation(abbr)
        variations = [f"{normalized}.png"]

        # Add common variations
        if normalized == "TA&M":
            variations.append("TAANDM.png")
        elif normalized == "TAMU":
            variations.append("TA&M.png")

        return variations

_downloader = LogoDownloader()

def download_missing_logo(sport_key: str, team_id: str, team_abbr: str, logo_path: Path, logo_url: Optional[str] = None) -> bool:
    """
    Download missing logo for a team.

    Args:
        sport_key: Sport key (e.g., 'nfl', 'ncaa_fb')
        team_id: Team ID
        team_abbr: Team abbreviation
        logo_path: Path where logo should be saved
        logo_url: Optional logo URL

    Returns:
        True if logo was downloaded successfully, False otherwise
    """
    try:
        # Ensure directory exists and is writable
        logo_dir = logo_path.parent
        try:
            logo_dir.mkdir(parents=True, exist_ok=True)

            # Check if we can write to the directory
            test_file = logo_dir / '.write_test'
            try:
                test_file.touch()
                test_file.unlink()
            except PermissionError:
                logger.error(f"Permission denied: Cannot write to directory {logo_dir}")
                logger.error("Please run: sudo ./scripts/fix_perms/fix_assets_permissions.sh")
                return False
        except PermissionError as e:
            logger.error(f"Permission denied: Cannot create directory {logo_dir}: {e}")
            logger.error("Please run: sudo ./scripts/fix_perms/fix_assets_permissions.sh")
            return False
        except Exception as e:
            logger.error(f"Failed to create logo directory {logo_dir}: {e}")
            return False

        if logo_url and not logo_url.lower().startswith("https://"):
            logger.warning(f"Skipping non-HTTPS logo URL for {team_abbr}: {logo_url}")
            logo_url = None
        if not logo_url:
            return False
        return _fetch_logo(logo_url, logo_path, team_abbr)

    except PermissionError as e:
        logger.error(f"Permission denied for {team_abbr}: {e}")
        logger.error("Please run: sudo ./scripts/fix_perms/fix_assets_permissions.sh")
        return False
    except Exception as e:
        logger.error(f"Failed to download logo for {team_abbr}: {e}")
        return False


#: Largest logo download accepted. ESPN's are 500x500 PNGs of tens of KB.
MAX_LOGO_BYTES = 2 * 1024 * 1024


def _fetch_logo(logo_url: str, logo_path: Path, team_abbr: str) -> bool:
    """Download ``logo_url`` to ``logo_path`` as an RGBA PNG.

    Nothing is written to ``logo_path`` unless the bytes are an image Pillow
    can decode: the download goes to a temp file beside it and is moved into
    place only once converted. A failure leaves no file -- not a placeholder,
    which used to be saved under the real logo's name, so the "already have
    it" check never let the logo be fetched again. The renderer draws its own
    placeholder for a team without a crest.
    """
    tmp = logo_path.with_name(logo_path.name + ".part")
    try:
        with _downloader.session.get(logo_url, headers={**_downloader.headers, "Accept": "image/*"},
                                     timeout=_downloader.request_timeout, stream=True) as response:
            if response.status_code != 200:
                logger.warning(f"Logo for {team_abbr} not downloaded: HTTP {response.status_code}")
                return False
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                logger.warning(f"Logo for {team_abbr} is not an image ({content_type!r})")
                return False
            received = 0
            with open(tmp, "wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    received += len(chunk)
                    if received > MAX_LOGO_BYTES:
                        raise ValueError("logo larger than the download limit")
                    f.write(chunk)
        with Image.open(tmp) as img:
            img.load()
            rgba = img.convert("RGBA")
        rgba.save(tmp, "PNG")
        tmp.replace(logo_path)
        logger.info(f"Downloaded logo for {team_abbr} from {logo_url}")
        return True
    except PermissionError as e:
        logger.error(f"Permission denied downloading logo for {team_abbr}: {e}")
        logger.error("Please run: sudo ./scripts/fix_perms/fix_assets_permissions.sh")
        return False
    except Exception as e:
        logger.warning(f"Failed to download logo for {team_abbr}: {e}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


def is_placeholder_logo(path: Path) -> bool:
    """Whether ``path`` holds a placeholder that create_placeholder_logo
    saved under a real logo's name (plugin 1.3.3 and earlier did, on a failed
    download): 64x64 and nothing in it but transparency and greys (white text,
    black outline, anti-aliased edges between)."""
    try:
        with Image.open(path) as img:
            if img.size != (64, 64):
                return False
            colours = img.convert("RGBA").getcolors(64 * 64) or []
    except Exception:
        return False
    return all(a == 0 or r == g == b for _count, (r, g, b, a) in colours)


def create_placeholder_logo(team_abbr: str, logo_path: Path) -> bool:
    """Create a simple placeholder logo."""
    try:
        # Ensure directory exists
        logo_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a simple text-based logo
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Try to load a font
        try:
            font = ImageFont.truetype("assets/fonts/PressStart2P-Regular.ttf", 16)
        except (IOError, OSError):
            font = ImageFont.load_default()

        # Draw team abbreviation
        text = team_abbr[:3]  # Limit to 3 characters
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = (64 - text_width) // 2
        y = (64 - text_height) // 2

        # Draw white text with black outline
        for dx, dy in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=(255, 255, 255))

        # Save the placeholder
        img.save(logo_path)
        logger.info(f"Created placeholder logo for {team_abbr}")
        return True

    except PermissionError as e:
        logger.error(f"Permission denied creating placeholder logo for {team_abbr}: {e}")
        logger.error("Please run: sudo ./scripts/fix_perms/fix_assets_permissions.sh")
        return False
    except Exception as e:
        logger.error(f"Failed to create placeholder logo for {team_abbr}: {e}")
        return False
