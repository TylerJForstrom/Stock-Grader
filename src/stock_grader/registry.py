"""Decorator-based plugin registries.

Every extensible layer — metrics, normalizers, aggregators, weighting methods, graders, providers —
registers itself by name here, so adding a new weighting method is a matter of dropping a decorated
function into ``weighting/`` and nothing else. The CLI's introspection commands (``stock-grader
methods``, ``stock-grader metrics``) read straight off these registries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

__all__ = [
    "AGGREGATORS",
    "GRADERS",
    "METRICS",
    "NORMALIZERS",
    "PROFILES",
    "PROVIDERS",
    "WEIGHTINGS",
    "MetricSpec",
    "Registry",
    "aggregator",
    "grader",
    "metric",
    "normalizer",
    "provider",
    "weighting",
]

T = TypeVar("T")


class Registry(Generic[T]):
    """A name -> object map with a decorator interface and duplicate protection."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, obj: T, *, overwrite: bool = False) -> T:
        if name in self._items and not overwrite:
            raise ValueError(f"duplicate {self.kind} name {name!r}")
        self._items[name] = obj
        return obj

    def __call__(self, name: str, **attrs: Any) -> Callable[[T], T]:
        """Use as a decorator: ``@WEIGHTINGS("entropy", needs_panel=True)``."""

        def decorate(obj: T) -> T:
            for key, value in attrs.items():
                try:
                    setattr(obj, key, value)
                except AttributeError:
                    pass
            try:
                obj.name = name
            except AttributeError:
                pass
            return self.register(name, obj)

        return decorate

    def get(self, name: str) -> T:
        if name not in self._items:
            close = ", ".join(sorted(self._items)[:8])
            raise KeyError(f"unknown {self.kind} {name!r}; registered: {close}...")
        return self._items[name]

    def maybe(self, name: str) -> T | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> Iterator[tuple[str, T]]:
        yield from sorted(self._items.items())

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)


@dataclass(slots=True)
class MetricSpec:
    """A registered metric: how to compute it and how to interpret the result."""

    name: str
    pillar: str
    direction: int  # +1 higher-is-better, -1 lower-is-better, 0 non-monotonic ideal band
    fn: Callable[..., Any]
    description: str = ""
    unit: str = ""
    needs_prices: bool = False
    needs_benchmark: bool = False
    needs_risk_free: bool = False
    min_history: int = 0  # trading days of price history required
    winsor: tuple[float, float] | None = None  # absolute clamp before scoring
    ideal_band: tuple[float, float] | None = None  # for direction == 0
    tags: frozenset[str] = field(default_factory=frozenset)
    # Redundancy group: metrics in the same group are close restatements of one
    # signal (a ratio and its reciprocal, three FCF multiples). At weighting
    # time the group shares ONE metric's worth of weight, split equally among
    # its live members — otherwise the effective weight of a signal is set by
    # how many correlated proxies of it someone happened to register.
    group: str | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


METRICS: Registry[MetricSpec] = Registry("metric")
NORMALIZERS: Registry[Callable] = Registry("normalizer")
AGGREGATORS: Registry[Callable] = Registry("aggregator")
WEIGHTINGS: Registry[Any] = Registry("weighting method")
GRADERS: Registry[Any] = Registry("grader")
PROVIDERS: Registry[Any] = Registry("provider")
PROFILES: Registry[Any] = Registry("profile")


def metric(
    name: str,
    *,
    pillar: str,
    direction: int = 1,
    description: str = "",
    unit: str = "",
    needs_prices: bool = False,
    needs_benchmark: bool = False,
    needs_risk_free: bool = False,
    min_history: int = 0,
    winsor: tuple[float, float] | None = None,
    ideal_band: tuple[float, float] | None = None,
    tags: frozenset[str] | set[str] | None = None,
    group: str | None = None,
) -> Callable[[Callable], MetricSpec]:
    """Register a metric function.

    The wrapped function receives a :class:`~stock_grader.types.SecuritySnapshot` and returns a
    float, or ``None`` when the inputs it needs are absent. Returning ``None`` is the correct
    response to missing data — never a zero, never a sentinel, because a zero flows silently into
    the score and a sentinel poisons the cross-sectional distribution.
    """

    def decorate(fn: Callable) -> MetricSpec:
        spec = MetricSpec(
            name=name,
            pillar=pillar,
            direction=direction,
            fn=fn,
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
            unit=unit,
            needs_prices=needs_prices,
            needs_benchmark=needs_benchmark,
            needs_risk_free=needs_risk_free,
            min_history=min_history,
            winsor=winsor,
            ideal_band=ideal_band,
            tags=frozenset(tags or ()),
            group=group,
        )
        METRICS.register(name, spec)
        return spec

    return decorate


normalizer = NORMALIZERS
aggregator = AGGREGATORS
weighting = WEIGHTINGS
grader = GRADERS
provider = PROVIDERS
