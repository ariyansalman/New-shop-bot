"""What a customer sees on the shopfront.

The stock line is the part that was actually wrong rather than merely
plain: file products carry UNLIMITED_STOCK instead of a real count, so
every screen told buyers "In Stock: 999999".
"""

from decimal import Decimal

import pytest

from database import UNLIMITED_STOCK
from utils import (
    format_stock, format_product_display, build_availability_text,
    LOW_STOCK_THRESHOLD, read_image_bytes,
)
from utils.keyboards import create_product_detail_keyboard


class FakeProduct:
    def __init__(self, name, price, stock, description=None):
        self.name = name
        self.price = Decimal(price)
        self.stock_count = stock
        self.description = description


# ----------------------------------------------------------- stock line

def test_a_file_product_never_shows_its_sentinel_count():
    """The bug: "In Stock: 999999" on every screen selling a file."""
    text = format_stock(UNLIMITED_STOCK)

    assert "999999" not in text
    assert text == "✅ Instant delivery"


def test_sold_out_is_stated_plainly():
    assert format_stock(0) == "❌ Sold out"
    assert format_stock(None) == "❌ Sold out"


def test_a_low_count_is_named():
    assert format_stock(LOW_STOCK_THRESHOLD) == f"⚠️ Only {LOW_STOCK_THRESHOLD} left"
    assert format_stock(1) == "⚠️ Only 1 left"


def test_a_healthy_count_is_shown():
    assert format_stock(LOW_STOCK_THRESHOLD + 1) == f"✅ In stock ({LOW_STOCK_THRESHOLD + 1})"


# -------------------------------------------------------- product page

def test_the_product_page_leads_with_the_name():
    text = format_product_display(FakeProduct("Netflix Premium", "10.00", 47))

    assert text.startswith("📦 Netflix Premium")
    assert "Name:" not in text          # the label was noise
    assert "$10.00" in text


def test_the_description_only_appears_when_asked_for():
    product = FakeProduct("Netflix", "10.00", 47, "Works everywhere")

    assert "Works everywhere" not in format_product_display(product)
    assert "Works everywhere" in format_product_display(product, include_description=True)


def test_a_product_with_no_description_gains_no_empty_section():
    text = format_product_display(FakeProduct("Netflix", "10.00", 47),
                                  include_description=True)

    assert "📝" not in text
    assert not text.endswith("\n")


# ------------------------------------------------------- the buy button

def test_a_sold_out_product_does_not_offer_buy_now():
    """The purchase flow already refuses it - but only after the customer
    has tapped and waited."""
    labels = [b.text for row in
              create_product_detail_keyboard(1, in_stock=False).inline_keyboard
              for b in row]

    assert "❌ Sold Out" in labels
    assert "🛒 Buy Now" not in labels


def test_an_in_stock_product_offers_buy_now():
    labels = [b.text for row in
              create_product_detail_keyboard(1, in_stock=True).inline_keyboard
              for b in row]

    assert "🛒 Buy Now" in labels


def test_the_buy_callback_is_unchanged_either_way():
    """A sold-out tap must still reach the handler that explains why."""
    for in_stock in (True, False):
        data = [b.callback_data for row in
                create_product_detail_keyboard(7, in_stock=in_stock).inline_keyboard
                for b in row]
        assert "buy_7" in data


# --------------------------------------------------------- availability

def test_availability_never_prints_the_sentinel():
    text = build_availability_text({
        "Design": [FakeProduct("Preset Pack", "4.00", UNLIMITED_STOCK)],
    })

    assert "999999" not in text
    assert "Instant delivery" in text


def test_availability_groups_by_category():
    text = build_availability_text({
        "Streaming": [FakeProduct("Netflix", "10.00", 47)],
        "Design": [FakeProduct("Presets", "4.00", 2)],
    })

    assert "▸ Streaming" in text
    assert "▸ Design" in text
    assert "⚠️ Only 2 left" in text


# ------------------------------------------------------------- images

async def test_a_missing_image_reads_as_none(tmp_path):
    """A redeploy wipes ASSETS_DIR, so this is the normal case, not an edge."""
    assert await read_image_bytes(str(tmp_path / "gone.jpg")) is None
    assert await read_image_bytes(None) is None
    assert await read_image_bytes("") is None


async def test_an_image_is_read_as_bytes(tmp_path):
    path = tmp_path / "logo.jpg"
    path.write_bytes(b"\xff\xd8\xff-not-really-a-jpeg")

    assert await read_image_bytes(str(path)) == b"\xff\xd8\xff-not-really-a-jpeg"


async def test_a_directory_is_not_an_image(tmp_path):
    """open() on a directory raises IsADirectoryError, an OSError."""
    assert await read_image_bytes(str(tmp_path)) is None


@pytest.mark.parametrize("count", [0, 1, 5, 6, 100, UNLIMITED_STOCK])
def test_every_stock_value_produces_a_line(count):
    text = format_stock(count)
    assert text and "999999" not in text
