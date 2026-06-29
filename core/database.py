"""Database management for storing detection results."""

import logging
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)
Base = declarative_base()


class DoorDetectionResult(Base):
    """ORM model for detection results."""

    __tablename__ = "door_detection_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    door_number = Column(String(50))
    confidence = Column(Float)
    heading = Column(Integer)
    pitch = Column(Integer)
    timestamp = Column(DateTime, default=datetime.now)
    raw_ocr_text = Column(Text)
    status = Column(String(20), default="success")
    error_message = Column(Text)


class DatabaseManager:
    """Handles database operations."""

    def __init__(self, config):
        self.config = config
        self.engine = self._create_engine()
        self.Session = sessionmaker(bind=self.engine)
        self._init_db()

    def _create_engine(self):
        """Create a SQLAlchemy engine based on configuration."""
        db_type = self.config.db_type

        try:
            if db_type == "oracle":
                connection_string = self._oracle_connection_string()
            elif db_type == "postgresql":
                connection_string = self._postgresql_connection_string()
            elif db_type == "mysql":
                connection_string = self._mysql_connection_string()
            elif db_type == "sqlite":
                connection_string = "sqlite:///door_detector.db"
            else:
                raise ValueError(f"Unknown database type: {db_type}")

            logger.info(f"Connecting to {db_type} database...")
            return create_engine(connection_string, echo=self.config.debug)

        except Exception as exc:
            logger.error(f"Error creating engine: {exc}")
            raise

    def _oracle_connection_string(self):
        """Build the Oracle connection string."""
        return (
            f"oracle+cx_Oracle://{self.config.db_user}:{self.config.db_password}"
            f"@{self.config.db_host}:{self.config.db_port}/?service_name={self.config.db_sid}"
        )

    def _postgresql_connection_string(self):
        """Build the PostgreSQL connection string."""
        return (
            f"postgresql://{self.config.db_user}:{self.config.db_password}"
            f"@{self.config.db_host}:{self.config.db_port}/{self.config.db_name}"
        )

    def _mysql_connection_string(self):
        """Build the MySQL connection string."""
        return (
            f"mysql+pymysql://{self.config.db_user}:{self.config.db_password}"
            f"@{self.config.db_host}:{self.config.db_port}/{self.config.db_name}"
        )

    def _init_db(self):
        """Initialize database tables."""
        try:
            Base.metadata.create_all(self.engine)
            logger.info("Database tables created or verified successfully")
        except Exception as exc:
            logger.error(f"Error creating tables: {exc}")
            raise

    def save_result(self, result):
        """Persist a detection result."""
        session = self.Session()
        try:
            detection = DoorDetectionResult(
                latitude=result["latitude"],
                longitude=result["longitude"],
                door_number=result.get("door_number"),
                confidence=result.get("confidence", 0),
                heading=result.get("heading"),
                pitch=result.get("pitch"),
                timestamp=datetime.now(),
                status="success" if result["success"] else "error",
                error_message=result.get("error"),
            )

            session.add(detection)
            session.commit()
            logger.info(f"Result saved with ID: {detection.id}")

        except Exception as exc:
            session.rollback()
            logger.error(f"Error saving result: {exc}")
            raise
        finally:
            session.close()

    def get_results(self, limit=100):
        """Retrieve the latest detection results."""
        session = self.Session()
        try:
            results = (
                session.query(DoorDetectionResult)
                .order_by(DoorDetectionResult.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [self._to_dict(result) for result in results]
        finally:
            session.close()

    def _to_dict(self, obj):
        """Convert an ORM object into a dictionary."""
        return {
            "id": obj.id,
            "latitude": obj.latitude,
            "longitude": obj.longitude,
            "door_number": obj.door_number,
            "confidence": obj.confidence,
            "timestamp": obj.timestamp.isoformat(),
            "status": obj.status,
        }

    def close(self):
        """Close database connections."""
        self.engine.dispose()
        logger.info("Database connection closed")
