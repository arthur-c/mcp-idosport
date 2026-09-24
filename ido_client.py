"""Compatibility imports for existing local users."""

from mcp_idosport.client import (
    IdOAuthenticationError,
    IdOClient,
    IdOClientError,
    IdOParseError,
    IdOUpstreamError,
    IdOValidationError,
    normalize_date_range,
)

__all__ = [
    "IdOAuthenticationError",
    "IdOClient",
    "IdOClientError",
    "IdOParseError",
    "IdOUpstreamError",
    "IdOValidationError",
    "normalize_date_range",
]
