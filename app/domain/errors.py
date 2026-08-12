"""Domain-level failures that must make a recommendation unavailable."""


class DomainError(ValueError):
    """Base class for invalid or incomplete deterministic inputs."""


class CalculationUnavailable(DomainError):
    """The leverage calculation cannot be completed safely."""


class SizingUnavailable(DomainError):
    """MES sizing cannot be completed safely."""


class ScheduleUnavailable(DomainError):
    """The current scheduled target cannot be reconstructed safely."""


class EventStateInconsistent(DomainError):
    """The event overlay contains contradictory or incomplete state."""
