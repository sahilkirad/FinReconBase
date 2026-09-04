from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # NoDecode: pydantic-settings would otherwise json.loads() these
    # complex fields before validation; the comma-separated env format
    # (e.g. ".pdf,.jpg") is parsed by the validator below instead.
    allowed_upload_extensions: Annotated[list[str], NoDecode] = Field(default=['.pdf', '.jpg', '.jpeg', '.png'])
    allowed_upload_mime_types: Annotated[list[str], NoDecode] = Field(default=['application/pdf', 'image/jpeg', 'image/png'])
    allowed_batch_extensions: Annotated[list[str], NoDecode] = Field(default=['.pdf', '.jpg', '.jpeg', '.png'])

    @field_validator(
        'allowed_upload_extensions',
        'allowed_upload_mime_types',
        'allowed_batch_extensions',
        mode='before',
    )
    @classmethod
    def _parse_list_from_env(cls, v):
        # Accept both comma-separated strings (env convention) and JSON arrays.
        if isinstance(v, str):
            return [item.strip() for item in v.split(',') if item.strip()]
        return v

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

    # VLM request timeout (seconds). Dense OCR on the free tier routinely
    # exceeds 60s; let slow calls finish instead of aborting them (a client
    # DeadlineExceeded is NOT retried today - it kills the page in one shot).
    gemini_request_timeout_s: int = Field(default=300, ge=30, le=600)

    # Worker configuration
    layer1_consumer_group: str = Field(default='layer1_extractor_group')
    layer1_max_concurrent: int = Field(default=3)

    # Layer 2: Reconciliation Supervisor (LangGraph / Groq)
    groq_rpm_limit: int = Field(default=28)  # Slight buffer under Groq's 30 RPM free tier
    layer2_consumer_group: str = Field(default='layer2-supervisor-cg')
    layer2_max_concurrent: int = Field(default=4)  # Isolated execution pool size
    layer2_poll_interval_s: float = Field(default=3.0)  # DB boundary poller cadence
    layer2_buffer_grace_polls: int = Field(default=10)  # Sealed-batch buffer drain grace
    layer2_tds_category: str = Field(default='194C')  # Deterministic TDS slab for waterfall

    # Demo auto-feed generator (POST /demo/auto-generate-feeds): bounds for the
    # server-side wait that lands Streams 2 & 3 before the Layer 2 seal.
    auto_feed_wait_s: int = Field(default=900, ge=30, le=3600)
    auto_feed_poll_s: float = Field(default=3.0, ge=1.0, le=60.0)

    # Layer 5: Ledger Writer (immutable double-entry sink)
    layer5_consumer_group: str = Field(default='layer5-ledger-writer-cg')
    layer5_exception_consumer_group: str = Field(default='layer5-exception-materializer-cg')
    ledger_fatal_dlq_topic: str = Field(default='ledger.fatal.dlq.events')

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
