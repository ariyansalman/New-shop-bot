"""Database initialization script with default data."""

import logging

from database.db import get_db_session, init_db, check_connection
from database.models import Settings

logger = logging.getLogger(__name__)


def create_default_settings():
    """Create default settings record if it doesn't exist."""
    with get_db_session() as session:
        settings = session.query(Settings).first()
        if not settings:
            settings = Settings(
                welcome_message="Welcome to our Digital Products Store!\n\nBrowse our collection of premium software keys and digital downloads.",
                support_username="",
                channel_username=""
            )
            session.add(settings)
            logger.info("Default settings created")
        else:
            logger.info("Settings already exist")


def initialize_database():
    """Initialize database with tables and default data."""
    logger.info("Initializing database...")
    if not check_connection():
        raise RuntimeError("Database is unreachable - see the error above")
    init_db()
    create_default_settings()
    logger.info("Database initialization complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    initialize_database()
