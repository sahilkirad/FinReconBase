from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = 'Fintech Reconciliation Backend'
    environment: str = Field(default='local')

    database_url: str = Field(
        default='postgresql+psycopg://postgres:postgres@localhost:5457/finrecon'
    )

    google_oauth_client_id: str = Field()

    jwt_secret_key: str = Field()
    jwt_algorithm: str = Field(default='HS256')
    jwt_expire_minutes: int = Field(default=15)

    kafka_bootstrap_servers: str = Field(default='kafka:9093')
    kafka_security_protocol: str = Field(default='SSL')
    kafka_ssl_ca_location: str = Field(default='/app/certs/ca.crt')
    kafka_ssl_certificate_location: str = Field(default='/app/certs/client.crt')
    kafka_ssl_key_location: str = Field(default='/app/certs/client.key')

    groq_api_key: str = Field()
    groq_model: str = Field()

    # Layer 1: Ingestion & Extraction
    gemini_api_key: str = Field(default='replace-with-gemini-api-key')
    gemini_model_fast: str = Field(default='gemini-3.5-flash-lite')  # Standard quality
    gemini_model_fallback: str = Field(default='gemini-3.7-flash')  # Degraded images

    # OCR engine: 'tesseract' or 'textract' (mock for local)
    ocr_engine: str = Field(default='tesseract')

    # OCR confidence threshold for model routing
    ocr_confidence_threshold: float = Field(default=70.0)

    max_upload_size_mb: int = Field(default=10)
    max_batch_size_mb: int = Field(default=100)
    allowed_upload_extensions: list[str] = Field(default=['.pdf', '.jpg', '.jpeg', '.png'])
    allowed_upload_mime_types: list[str] = Field(default=['application/pdf', 'image/jpeg', 'image/png'])
    allowed_batch_extensions: list[str] = Field(default=['.pdf', '.csv'])

    # Blur detection threshold (Laplacian variance)
    blur_threshold: float = Field(default=100.0)
    # Enable blur sharpening attempt before failing
    blur_sharpen_enabled: bool = Field(default=True)

    # Document classification threshold
    classification_threshold: float = Field(default=0.80)

    # Batch storage path (shared Docker volume)
    batch_storage_path: str = Field(default='/app/data/batch_files')

    # Redis for distributed rate limiting
    redis_url: str = Field(default='redis://redis:6379/0')
    gemini_rpm_limit: int = Field(default=15)

    # Worker configuration
    layer1_consumer_group: str = Field(default='layer1_extractor_group')
    layer1_max_concurrent: int = Field(default=3)

    # Kafka topics
    raw_ingestion_topic: str = Field(default='invoice.processing.events')
    invoice_extracted_topic: str = Field(default='invoice.extracted.events')
    reconciliation_completed_topic: str = Field(default='reconciliation.completed.events')
    reconciliation_dlq_topic: str = Field(default='reconciliation.dlq.events')

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
