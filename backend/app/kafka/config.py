"""
Kafka Configuration

Centralizes Kafka connection settings, topic names, and consumer group configs.
Reads from environment variables via pydantic-settings.
"""

import logging
from dataclasses import dataclass

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# --- Topic Names ---
INVOICE_PROCESSING_TOPIC = "invoice.processing.events"
INVOICE_EXTRACTED_TOPIC = "invoice.extracted.events"
RECONCILIATION_COMPLETED_TOPIC = "reconciliation.completed.events"
RECONCILIATION_DLQ_TOPIC = "reconciliation.dlq.events"


@dataclass
class KafkaConfig:
    """Kafka connection and topic configuration."""
    
    # Connection
    bootstrap_servers: str = "kafka:9093"
    security_protocol: str = "SSL"
    
    # SSL (for production)
    ssl_cafile: str | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    
    # Consumer groups
    invoice_consumer_group: str = "finrecon-invoice-processor"
    
    # Topics
    invoice_processing_topic: str = INVOICE_PROCESSING_TOPIC
    invoice_extracted_topic: str = INVOICE_EXTRACTED_TOPIC
    reconciliation_completed_topic: str = RECONCILIATION_COMPLETED_TOPIC
    reconciliation_dlq_topic: str = RECONCILIATION_DLQ_TOPIC
    
    # Processing limits
    max_concurrent_vlm_calls: int = 10
    batch_size: int = 50
    
    @classmethod
    def from_settings(cls) -> "KafkaConfig":
        """Load config from environment settings."""
        settings = get_settings()
        return cls(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            ssl_cafile=settings.kafka_ssl_ca_location,
            ssl_certfile=settings.kafka_ssl_certificate_location,
            ssl_keyfile=settings.kafka_ssl_key_location,
        )
    
    def get_producer_config(self) -> dict:
        """Get kafka-python producer configuration."""
        config = {
            "bootstrap_servers": self.bootstrap_servers,
            "client_id": "finrecon-producer",
            "acks": "all",  # Wait for all replicas
            "retries": 3,
            "retry_backoff_ms": 100,
            "linger_ms": 10,  # Batch for efficiency
            "batch_size": 16384,
        }
        
        if self.security_protocol == "SSL":
            config.update({
                "security_protocol": "SSL",
                "ssl_cafile": self.ssl_cafile,
                "ssl_certfile": self.ssl_certfile,
                "ssl_keyfile": self.ssl_keyfile,
            })
        elif self.security_protocol == "PLAINTEXT":
            config["security_protocol"] = "PLAINTEXT"
        
        return config
    
    def get_consumer_config(self, group_id: str | None = None) -> dict:
        """Get kafka-python consumer configuration."""
        config = {
            "bootstrap_servers": self.bootstrap_servers,
            "group_id": group_id or self.invoice_consumer_group,
            "auto_offset_reset": "earliest",
            "enable_auto_commit": False,  # Manual commit after processing
            "max_poll_records": 10,
        }
        
        if self.security_protocol == "SSL":
            config.update({
                "security_protocol": "SSL",
                "ssl_cafile": self.ssl_cafile,
                "ssl_certfile": self.ssl_certfile,
                "ssl_keyfile": self.ssl_keyfile,
            })
        elif self.security_protocol == "PLAINTEXT":
            config["security_protocol"] = "PLAINTEXT"
        
        return config
