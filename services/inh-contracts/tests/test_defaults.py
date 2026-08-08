"""Golden default-value test -- the package is the source of truth (#132, #176).

DEFAULT_S3_REGION is the single default both services' Settings classes must
import for their S3/object-store region field. Pinning the literal here means
a future accidental edit to this constant is a visible, reviewed diff instead
of a silent redefinition on one service's side. See inh_contracts.defaults
for the full defect writeup.

DEFAULT_S3_BUCKET and DEFAULT_MONGODB_URI (#176) are the same pattern for the
bucket name and mongodb_uri: both services' Settings classes import them, so
this package -- the actual source of truth -- needs its own golden-value and
re-export tests too, not just each service's contract test. Without these,
this package could carry a wrong or accidentally-edited value and every
downstream contract test would still pass (they only check that both services
agree WITH EACH OTHER, not that the shared value itself is right).
"""

from inh_contracts import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET, DEFAULT_S3_REGION
from inh_contracts.defaults import DEFAULT_MONGODB_URI as DEFAULT_MONGODB_URI_DIRECT
from inh_contracts.defaults import DEFAULT_S3_BUCKET as DEFAULT_S3_BUCKET_DIRECT
from inh_contracts.defaults import DEFAULT_S3_REGION as DEFAULT_S3_REGION_DIRECT


def test_default_s3_region_golden_value() -> None:
    """Pin the shared default so changing it is a deliberate, reviewed edit."""
    assert DEFAULT_S3_REGION == "us-east-1"


def test_default_s3_region_reexported_from_package_root() -> None:
    """The top-level ``inh_contracts`` re-export must match the module value."""
    assert DEFAULT_S3_REGION == DEFAULT_S3_REGION_DIRECT


def test_default_s3_bucket_golden_value() -> None:
    """Pin the shared bucket default (#176) so changing it is deliberate."""
    assert DEFAULT_S3_BUCKET == "inherent-documents"


def test_default_s3_bucket_reexported_from_package_root() -> None:
    """The top-level ``inh_contracts`` re-export must match the module value."""
    assert DEFAULT_S3_BUCKET == DEFAULT_S3_BUCKET_DIRECT


def test_default_mongodb_uri_golden_value() -> None:
    """Pin the shared mongodb_uri default (#176) so changing it is deliberate.

    No database path segment on purpose -- mongodb_db_name is the actual
    source of truth for database selection on both services, not the URI
    path (see inh_contracts.defaults for the full writeup).
    """
    assert DEFAULT_MONGODB_URI == "mongodb://localhost:27017"


def test_default_mongodb_uri_reexported_from_package_root() -> None:
    """The top-level ``inh_contracts`` re-export must match the module value."""
    assert DEFAULT_MONGODB_URI == DEFAULT_MONGODB_URI_DIRECT
