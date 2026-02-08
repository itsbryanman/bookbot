"""Provider health monitoring system."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.logging import get_logger

logger = get_logger("provider_health")


@dataclass
class HealthStats:
    """Provider health statistics."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_response_time: float = 0.0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=10))

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    @property
    def is_healthy(self) -> bool:
        """Healthy if >50% success rate in last 100 requests."""
        if self.total_requests < 5:
            return True  # Not enough data
        return self.success_rate > 0.5


class ProviderHealthMonitor:
    """Track provider reliability."""

    def __init__(self, window_minutes: int = 5):
        self.window = timedelta(minutes=window_minutes)
        self.stats: dict[str, HealthStats] = {}

    def record_success(self, provider: str, response_time: float) -> None:
        """Record successful request."""
        if provider not in self.stats:
            self.stats[provider] = HealthStats()

        stats = self.stats[provider]
        stats.total_requests += 1
        stats.successful_requests += 1
        stats.last_success = datetime.now()

        # Update average response time (running average)
        n = stats.successful_requests
        stats.avg_response_time = (
            stats.avg_response_time * (n - 1) + response_time
        ) / n

        logger.debug(
            f"Provider {provider} success",
            response_time=response_time,
            success_rate=stats.success_rate,
        )

    def record_failure(self, provider: str, error: str) -> None:
        """Record failed request."""
        if provider not in self.stats:
            self.stats[provider] = HealthStats()

        stats = self.stats[provider]
        stats.total_requests += 1
        stats.failed_requests += 1
        stats.last_failure = datetime.now()
        stats.recent_errors.append({"time": datetime.now(), "error": error})

        logger.warning(
            f"Provider {provider} failure",
            error=error,
            success_rate=stats.success_rate,
            is_healthy=stats.is_healthy,
        )

        if not stats.is_healthy:
            logger.error(
                f"Provider {provider} unhealthy",
                success_rate=stats.success_rate,
                total_requests=stats.total_requests,
            )

    def is_healthy(self, provider: str) -> bool:
        """Check if provider is healthy."""
        if provider not in self.stats:
            return True
        return self.stats[provider].is_healthy

    def get_stats(self, provider: str) -> HealthStats | None:
        """Get provider statistics."""
        return self.stats.get(provider)
