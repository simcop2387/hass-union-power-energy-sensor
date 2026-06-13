"""Exceptions for Union Power integration."""


class UnionPowerError(Exception):
    """Base class for Union Power errors."""


class UnionPowerConfigError(UnionPowerError):
    """Configuration error."""


class UnionPowerConnectionError(UnionPowerError):
    """Connection error."""


class UnionPowerAuthenticationError(UnionPowerError):
    """Authentication error."""


class UnionPowerDataError(UnionPowerError):
    """Data parsing error."""
