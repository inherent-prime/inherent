"""The upload-topic env var must match the ingestion service's (#15).

Public-api produces upload events; the ingestion service consumes them. They
must key the topic off the SAME env var, or overriding it on one side silently
routes uploads to a stream nobody consumes. Ingestion reads MQ_UPLOAD_TOPIC;
public-api must too.
"""

from __future__ import annotations

from src.config.settings import Settings


def test_upload_topic_reads_shared_env_var():
    field = Settings.model_fields["mq_topic_document_uploaded"]
    assert field.alias == "MQ_UPLOAD_TOPIC"
