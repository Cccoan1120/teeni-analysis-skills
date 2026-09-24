import re
from collections.abc import Mapping, Sequence
from typing import Any


class AggregatePrivacyError(ValueError):
    pass


FORBIDDEN_KEYS = re.compile(
    r"(^|_)(cid|clientid|client_id|text|ai_text|response|query|evidence|preview|fingerprint|birthday|city|city_normalized|constellation)(_|$)",
    re.IGNORECASE,
)
ALLOWED_AGGREGATE_KEYS = {
    "constellation_users",
    "constellation_eligible_users",
    "constellation_rate",
    "query_count",
}
IDENTIFIER_VALUE = re.compile(r"^[0-9a-f]{20,40}$")
SHA256_VALUE = re.compile(r"^[0-9a-f]{64}$")
EMAIL_VALUE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_VALUE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
HTML_TAG = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.IGNORECASE)
CONVERSATION_IDENTIFIER = re.compile(r"\b(clientid|client_id|cid)\b", re.IGNORECASE)


def assert_aggregate_only(payload: Any, path: str = "$") -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_text = str(key)
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key_text).lower()
            metric_ratio = (path == "$.metrics.supplemental.quality" and key_text in {"responseMissing", "responseParseFailure"}
                            and isinstance(value, Mapping) and set(value) == {"numerator", "denominator", "rate"}
                            and all(item is None or (isinstance(item, (int, float)) and not isinstance(item, bool)) for item in value.values()))
            missing_key_count = (path == "$.metrics.supplemental.inputQuality.missingKeys" and key_text in {"clientId", "cid"}
                                 and type(value) is int and value >= 0)
            early_metric = (re.fullmatch(r"\$\.metrics\.supplemental\.earlyExperience\[[0-3]\]", path)
                            and key_text in {"missingResponseSessions", "missingResponseSessionsRate"}
                            and (value is None or (type(value) in (int, float) and value >= 0)))
            if FORBIDDEN_KEYS.search(normalized) and normalized not in ALLOWED_AGGREGATE_KEYS and not metric_ratio and not missing_key_count and not early_metric:
                raise AggregatePrivacyError(f"forbidden field at {path}.{key_text}")
            assert_aggregate_only(value, f"{path}.{key_text}")
        return

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for index, value in enumerate(payload):
            assert_aggregate_only(value, f"{path}[{index}]")
        return

    if isinstance(payload, str):
        key_name = path.rsplit(".", 1)[-1].lower()
        if key_name.endswith("sha256") and SHA256_VALUE.fullmatch(payload):
            return
        if IDENTIFIER_VALUE.fullmatch(payload):
            raise AggregatePrivacyError(f"identifier-like value at {path}")
        if EMAIL_VALUE.search(payload) or PHONE_VALUE.search(payload):
            raise AggregatePrivacyError(f"contact-like value at {path}")
        if "\n" in payload or "\r" in payload or len(payload) > 240:
            raise AggregatePrivacyError(f"long or multiline text at {path}")


def assert_management_insight_text(value: str, path: str, max_length: int) -> None:
    if not isinstance(value, str):
        raise AggregatePrivacyError(f"text value is required at {path}")
    text = value.strip()
    if not text or len(text) > max_length:
        raise AggregatePrivacyError(f"text length is invalid at {path}")
    if IDENTIFIER_VALUE.search(text) or EMAIL_VALUE.search(text) or PHONE_VALUE.search(text):
        raise AggregatePrivacyError(f"identifier or contact-like value at {path}")
    if CONVERSATION_IDENTIFIER.search(text):
        raise AggregatePrivacyError(f"conversation identifier name at {path}")
    if HTML_TAG.search(text):
        raise AggregatePrivacyError(f"HTML is not allowed at {path}")
