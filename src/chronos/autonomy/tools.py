"""The bounded tool surface a model may call (ADR-0016 §3, M4).

The directive is explicit about what the reference project got wrong here: a
single registry mixing read-only lookups with **direct broker write functions**,
so the same dispatch that answered "what is SPY trading at" could also place an
order. That is the pattern this module is shaped to make impossible.

## Two kinds of tool, and only two

- **READ** tools answer questions from an already-issued
  :class:`~chronos.autonomy.evidence.EvidenceBundle`. They take the bundle and
  arguments, return data, and touch nothing else. They cannot reach a broker,
  a database, the filesystem, or the network, because they are given no handle
  to any of those — the bundle is the entire world they can see.
- **DECISION** tools do not act either. A decision tool's only effect is to
  produce a :class:`~chronos.autonomy.decision.ProposedDecision`, which is a
  value object that authorizes nothing and must still pass the whole
  deterministic gateway. Naming it a "tool" is a convenience for the model's
  interface; it is not a capability.

There is deliberately **no third kind**. There is no WRITE tool, no submit tool,
no cancel tool, no policy tool, no arming tool, and no deployment tool. Adding
one would require adding an enum member, which is exactly the reviewable act it
should be — and ``tests/safety/test_model_tool_surface.py`` fails if the enum
grows.

## What makes this structural rather than aspirational

A registry that merely *promises* to hold read-only functions is worth nothing;
the reference project's registry made the same promise. Three things make this
one different:

1. **The module imports nothing that can act.** ``chronos.autonomy`` may not
   import the order, broker, execution, risk, api, or persistence planes — an
   isolation test asserts it by AST walk and again by a subprocess import probe.
   So a tool handler defined here has no submission path in scope to call.
2. **Handlers receive the bundle and nothing else.** The signature is the
   sandbox: there is no session, no client, no config object to reach through.
3. **The registry is frozen after construction.** A tool cannot be added at
   runtime by code that happens to run later, which is how "read-only" surfaces
   usually stop being read-only.

## Honest bound

This constrains the *tool surface*. It does not sandbox Python: code running in
the same process could import whatever it likes regardless of what this module
does. The isolation the directive asks for — a model worker outside the
broker-writing process — is an operational deployment property, and Chronos does
not yet ship a process supervisor that enforces it. That gap is disclosed in
``docs/limitations.md`` and is a promotion criterion, not a solved problem.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chronos.autonomy.evidence import EvidenceBundle


class ToolKind(StrEnum):
    """What a tool is permitted to be. There is no write kind, by construction.

    Adding a member here is a deliberate, reviewable act with an ADR attached —
    and a safety test fails until this docstring and that test agree.
    """

    #: Answers a question from the issued evidence bundle. No side effects.
    READ = "READ"
    #: Produces a ProposedDecision, which authorizes nothing on its own.
    DECISION = "DECISION"


#: A read handler sees the bundle and its arguments. Nothing else is in scope:
#: no session, no broker, no settings, no filesystem.
ReadHandler = Callable[[EvidenceBundle, Mapping[str, Any]], Any]


class ToolRegistryError(RuntimeError):
    """A tool could not be registered, or a call could not be dispatched."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool the model may call."""

    name: str
    kind: ToolKind
    description: str
    handler: ReadHandler

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ToolRegistryError(
                f"tool name {self.name!r} must be a simple identifier; a name that can "
                "carry punctuation is a name that can carry an injection"
            )
        if not self.description.strip():
            raise ToolRegistryError(
                f"tool {self.name!r} must describe itself; an undocumented tool in a "
                "model's surface is one nobody reviewed"
            )


class ToolRegistry:
    """A frozen, read-only tool surface.

    Frozen after :meth:`freeze` because a registry that can grow at runtime is a
    registry whose contents no test can pin. The model-facing dispatcher refuses
    to run against an unfrozen registry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._frozen = False

    def register(self, spec: ToolSpec) -> None:
        if self._frozen:
            raise ToolRegistryError(
                f"cannot register {spec.name!r}: the tool surface is frozen. A surface "
                "that grows after startup is one no test can pin."
            )
        if spec.name in self._tools:
            raise ToolRegistryError(
                f"tool {spec.name!r} is already registered; silently replacing a tool "
                "would let a later import redefine what an earlier review approved"
            )
        self._tools[spec.name] = spec

    def freeze(self) -> ToolRegistry:
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name] for name in self.names())

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def call(self, name: str, bundle: EvidenceBundle, arguments: Mapping[str, Any]) -> Any:
        """Dispatch a tool call. Unknown names refuse rather than falling through.

        A dispatcher that ignored an unknown name would let a model probe the
        surface for free, and one that guessed at a near-match would let a typo
        — or an injected string — reach a different tool than the one named.
        """

        if not self._frozen:
            raise ToolRegistryError(
                "refusing to dispatch against an unfrozen tool surface: freeze the "
                "registry at startup so its contents are fixed and testable"
            )
        spec = self._tools.get(name)
        if spec is None:
            raise ToolRegistryError(
                f"unknown tool {name!r}; the model surface is an allowlist and there is no fallback"
            )
        return spec.handler(bundle, arguments)


# --------------------------------------------------------------- read tools
#
# Each of these answers from the bundle and nothing else. They are deliberately
# dull: the interesting safety property is what they cannot do.


def _quote(bundle: EvidenceBundle, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    symbol = str(arguments.get("symbol", "")).strip().upper()
    for quote in bundle.quotes:
        if quote.symbol == symbol:
            return quote.model_dump(mode="json")
    return None


def _positions(bundle: EvidenceBundle, arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    del arguments
    return [position.model_dump(mode="json") for position in bundle.positions]


def _account(bundle: EvidenceBundle, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    del arguments
    return bundle.account.model_dump(mode="json") if bundle.account else None


def _texts(bundle: EvidenceBundle, arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    del arguments
    # Returned with `untrusted` intact so the marking survives into the prompt.
    return [text.model_dump(mode="json") for text in bundle.texts]


def default_registry() -> ToolRegistry:
    """The shipped read-only surface, frozen.

    Four tools, all READ. If this list ever contains something that can act, the
    safety tests fail — which is the point.
    """

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="get_quote",
            kind=ToolKind.READ,
            description="The observed quote for one symbol, from the issued bundle.",
            handler=_quote,
        )
    )
    registry.register(
        ToolSpec(
            name="list_positions",
            kind=ToolKind.READ,
            description="Positions held, as shown in the issued bundle.",
            handler=_positions,
        )
    )
    registry.register(
        ToolSpec(
            name="get_account",
            kind=ToolKind.READ,
            description="Pseudonymous account state from the issued bundle.",
            handler=_account,
        )
    )
    registry.register(
        ToolSpec(
            name="list_texts",
            kind=ToolKind.READ,
            description="External text evidence, marked untrusted.",
            handler=_texts,
        )
    )
    return registry.freeze()
