from __future__ import annotations

from typing import Any

PRODUCT_LABEL_SEPARATOR = " \u2014 "


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"nan", "none", "null", "<na>"}:
        return None
    return text


def format_product_label(
    sku: Any,
    product_name: Any,
    *,
    fallback: str = "Unknown Product",
) -> str:
    sku_text = _clean_text(sku)
    name_text = _clean_text(product_name)
    if sku_text and name_text:
        if sku_text.casefold() == name_text.casefold():
            return sku_text
        if name_text.startswith(f"{sku_text}{PRODUCT_LABEL_SEPARATOR}"):
            return name_text
        return f"{sku_text}{PRODUCT_LABEL_SEPARATOR}{name_text}"
    if sku_text:
        return sku_text
    if name_text:
        return name_text
    return fallback


def compact_product_label(
    sku: Any,
    product_name: Any,
    *,
    max_length: int = 48,
    fallback: str = "Unknown Product",
) -> str:
    label = format_product_label(sku, product_name, fallback=fallback)
    if len(label) <= max_length:
        return label

    sku_text = _clean_text(sku)
    name_text = _clean_text(product_name)
    if sku_text and name_text:
        reserved = len(sku_text) + len(PRODUCT_LABEL_SEPARATOR) + 1
        available = max_length - reserved
        if available >= 8:
            return f"{sku_text}{PRODUCT_LABEL_SEPARATOR}{name_text[: available - 1].rstrip()}..."
    if max_length <= 4:
        return label[:max_length]
    return f"{label[: max_length - 3].rstrip()}..."
