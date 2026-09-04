"""Reading one page of rows out of the database.

Six list screens - users, orders, products to edit, categories,
subcategories, transactions - each loaded their whole table, built a tuple
per row, and then kept five. Every tap on Next did it again. At fifty rows
nobody notices; at five thousand it is five thousand rows and five
thousand objects per tap, on a screen an admin pages through repeatedly.

So the count and the slice happen in SQL. The page number is parsed here
too, because six copies of `int(query.data.split("_page_")[1])` is six
chances for a callback that does not match the shape to raise inside a
handler, where it becomes a button that silently does nothing.
"""

from dataclasses import dataclass

PAGE_SIZE = 5


def page_number(callback_data, default: int = 0) -> int:
    """The page a `..._page_N` callback is asking for. Never raises."""
    if not callback_data or "_page_" not in callback_data:
        return default
    try:
        return max(0, int(callback_data.rsplit("_page_", 1)[1]))
    except (TypeError, ValueError):
        return default


@dataclass
class Page:
    """One page of rows, and where it sits."""

    rows: list
    number: int
    total_pages: int
    total: int

    @property
    def has_previous(self) -> bool:
        return self.number > 0

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages - 1

    @property
    def label(self) -> str:
        return f"Page {self.number + 1}/{self.total_pages}"


def page_of(query, number: int, size: int = PAGE_SIZE) -> Page:
    """One page of a SQLAlchemy query. Call from a thread.

    A page past the end is clamped to the last one rather than rendered
    empty: that happens whenever rows are deleted while someone is paging,
    and an empty screen reads as breakage.
    """
    total = query.order_by(None).count()
    total_pages = max(1, (total + size - 1) // size)
    number = min(max(0, number), total_pages - 1)
    rows = query.limit(size).offset(number * size).all()
    return Page(rows=rows, number=number, total_pages=total_pages, total=total)
