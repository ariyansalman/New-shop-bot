"""Database models for the Telegram digital products store bot."""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Numeric, Boolean, DateTime,
    ForeignKey, Text, Enum, CheckConstraint, UniqueConstraint
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
from decimal import Decimal
import enum

Base = declarative_base()

# All money columns use this: fixed-precision storage (no binary-float
# drift at rest) and Python-side Decimal values (see utils/money.py -
# to_money() is what keeps arithmetic on those values exact too).
Money = Numeric(12, 2)


class ProductType(enum.Enum):
    """Enum for product types."""
    KEY = "key"
    FILE = "file"


class OrderStatus(enum.Enum):
    """Enum for order status."""
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class DisputeStatus(enum.Enum):
    """Enum for dispute status."""
    NIL = "NIL"
    OPENED = "Opened"
    RESOLVED = "Resolved"


class TransactionStatus(enum.Enum):
    """Enum for transaction/payment status.

    VERIFYING and MANUAL_REVIEW exist for provider-verified payments
    (currently Binance Pay): VERIFYING is held while a verification call is
    in flight, which is also what stops a second submission from starting a
    parallel verification of the same transaction. MANUAL_REVIEW is the
    terminal state for a payment that could not be settled automatically
    after the retry budget - it never credits on its own, an admin decides.
    """
    PENDING = "pending"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class PaymentMethod(enum.Enum):
    """Enum for payment methods."""
    CRYPTO_WALLET = "crypto_wallet"
    CARD = "card"
    BINANCE_PAY = "binance_pay"


class User(Base):
    """User model for storing customer information."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    # Telegram user IDs already exceed the signed 32-bit range -> BigInteger.
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255))
    wallet_balance = Column(Money, default=Decimal("0.00"), nullable=False)
    is_banned = Column(Boolean, default=False, nullable=False)
    # ISO 639-1 code; see utils/i18n.py's SUPPORTED_LANGS. Not DB-constrained
    # to that tuple - adding a language is a code change, not a migration.
    language = Column(String(10), default='en', nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint('wallet_balance >= 0', name='ck_users_wallet_balance_non_negative'),
    )

    # Relationships
    orders = relationship("Order", back_populates="user")
    cart_items = relationship("Cart", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user")


class Category(Base):
    """Category model for product organization."""
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    products = relationship("Product", back_populates="category")
    subcategories = relationship("Subcategory", back_populates="category")


class Subcategory(Base):
    """Subcategory model for additional product organization."""
    __tablename__ = 'subcategories'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    category = relationship("Category", back_populates="subcategories")
    products = relationship("Product", back_populates="subcategory")


class Product(Base):
    """Product model for items available for purchase."""
    __tablename__ = 'products'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    price = Column(Money, nullable=False)
    stock_count = Column(Integer, default=0)
    product_type = Column(Enum(ProductType), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    subcategory_id = Column(Integer, ForeignKey('subcategories.id'), nullable=True)
    image_path = Column(String(500), nullable=True)
    download_link = Column(String(500), nullable=True)  # For file-type products
    # Shown to the buyer alongside the key or link: how to redeem it, where
    # to enter it, what it will not work with. Optional - a product without
    # instructions delivers exactly as it did before.
    delivery_instructions = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    category = relationship("Category", back_populates="products")
    subcategory = relationship("Subcategory", back_populates="products")
    product_keys = relationship("ProductKey", back_populates="product", cascade="all, delete-orphan")
    cart_items = relationship("Cart", back_populates="product")
    order_items = relationship("OrderItem", back_populates="product")


class ProductKey(Base):
    """SEPARATE TABLE for storing product keys inventory."""
    __tablename__ = 'product_keys'

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    key_value = Column(Text, nullable=False)
    is_sold = Column(Boolean, default=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sold_at = Column(DateTime, nullable=True)

    # Relationships
    product = relationship("Product", back_populates="product_keys")
    order = relationship("Order", back_populates="assigned_keys")

    __table_args__ = (
        UniqueConstraint('product_id', 'key_value', name='uq_product_keys_product_key'),
    )


class Cart(Base):
    """Shopping cart model for temporary product storage."""
    __tablename__ = 'cart'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="cart_items")
    product = relationship("Product", back_populates="cart_items")


class Order(Base):
    """Order model for purchase records."""
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    total_amount = Column(Money, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PROCESSING)
    dispute_status = Column(Enum(DisputeStatus), default=DisputeStatus.NIL)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    order_items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    assigned_keys = relationship("ProductKey", back_populates="order")
    disputes = relationship("Dispute", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    """Order items model for individual line items in orders."""
    __tablename__ = 'order_items'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    # Nullable on purpose: an admin can delete a product that has already been
    # ordered, and the order line must survive that (it is the customer's
    # receipt). The display code on both sides already expects this - see
    # user_handlers.user_order_detail_callback ("Deleted product") and
    # admin_handlers._render_order_detail ("Unknown Product"). The line keeps
    # its own name-independent record: quantity, price paid, and the delivered
    # keys/link in delivered_asset.
    product_id = Column(Integer, ForeignKey('products.id'), nullable=True)
    quantity = Column(Integer, nullable=False)
    price = Column(Money, nullable=False)
    delivered_asset = Column(Text, nullable=True)  # Keys or download link
    # Copied from the product at purchase time, for the same reason
    # delivered_asset is: this line is the customer's receipt, and it has to
    # keep reading correctly after the product's instructions are edited or
    # the product itself is deleted.
    delivery_instructions = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    order = relationship("Order", back_populates="order_items")
    product = relationship("Product", back_populates="order_items")


class Transaction(Base):
    """Transaction model for wallet funding history."""
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    amount = Column(Money, nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    crypto_address = Column(String(500), nullable=True)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # --- Provider-verified payments (Binance Pay) -----------------------
    # These stay NULL for CRYPTO_WALLET and CARD, which settle through
    # their own existing paths and are untouched by this.
    #
    # provider_transaction_id is the id the *payer* submits (Binance's
    # transactionId). It is deliberately NOT the same thing as this row's
    # own `id`, which remains the local order number shown to the user.
    # The UNIQUE constraint is the real double-credit guard: even if two
    # requests race past the status check, only one can attach a given
    # provider transaction to a local one, so the same Binance payment can
    # never be credited twice - not by a double click, not by the user and
    # the background worker at once, not across two local transactions.
    provider = Column(String(32), nullable=True, index=True)
    provider_transaction_id = Column(String(128), nullable=True)
    verification_attempts = Column(Integer, default=0, nullable=False)
    last_verification_at = Column(DateTime, nullable=True)
    # Operator-facing failure reason. Never store credentials or raw
    # provider payloads here - it is shown in the admin panel.
    last_verification_error = Column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint('provider', 'provider_transaction_id',
                         name='uq_transactions_provider_txn'),
    )

    # Relationships
    user = relationship("User", back_populates="transactions")


class Settings(Base):
    """Settings model for store configuration (single row table)."""
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True)
    welcome_message = Column(Text, default="Welcome to our digital store!")
    store_logo_path = Column(String(500), nullable=True)
    support_username = Column(String(255), nullable=True)
    channel_username = Column(String(255), nullable=True)
    # Admin kill switch for Binance top-ups. It can only ever narrow what
    # the environment already allows: the method needs BINANCE_PAY_ENABLED
    # plus working credentials AND this flag. That way an admin can stop
    # accepting Binance payments from Telegram during an incident without a
    # redeploy, but cannot turn the method on for an account whose
    # credentials the server does not have.
    binance_pay_enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Broadcast(Base):
    """Broadcast model for tracking broadcast messages."""
    __tablename__ = 'broadcasts'

    id = Column(Integer, primary_key=True)
    message_text = Column(Text, nullable=False)
    image_path = Column(String(500), nullable=True)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Dispute(Base):
    """Dispute model for order disputes."""
    __tablename__ = 'disputes'

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    status = Column(Enum(DisputeStatus), default=DisputeStatus.OPENED)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    admin_notes = Column(Text, nullable=True)

    # Relationships
    order = relationship("Order", back_populates="disputes")
    user = relationship("User")


class AdminActionLog(Base):
    """Audit trail for moderation/money-moving admin actions.

    Written by utils.audit.log_admin_action(), called from the handlers that
    ban/unban, cancel or reactivate orders, confirm or cancel payments,
    resolve disputes, restock keys, and edit product prices. Not every admin
    action is logged (e.g. routine browsing) - this is for the ones an admin
    would plausibly need to explain later.
    """
    __tablename__ = 'admin_action_logs'

    id = Column(Integer, primary_key=True)
    # Telegram ID, not a users.id FK: the admin performing the action isn't
    # necessarily a row in `users` (that table is for customers), and we
    # want the log to stay readable even if that row is ever deleted.
    admin_telegram_id = Column(BigInteger, nullable=False, index=True)
    action = Column(String(100), nullable=False, index=True)
    target_type = Column(String(50), nullable=True)
    target_id = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
