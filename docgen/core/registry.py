"""Document type registry.

A document class declares the sources it supports; one variant is registered per source,
named "<doc_type>.<source>" (e.g. "invoice.adventureworks"):

    @register
    class Invoice(OrderDocument):
        doc_type = "invoice"
        sources = ["northwind", "adventureworks", "chinook"]
"""

from __future__ import annotations

import importlib
import pkgutil

_REGISTRY: dict[str, tuple[type, str]] = {}
_LOADED = False


def register(cls):
    if not cls.doc_type or not cls.sources:
        raise ValueError(f"{cls.__name__} must set doc_type and sources")
    for source in cls.sources:
        _REGISTRY[f"{cls.doc_type}.{source}"] = (cls, source)
    return cls


def load_document_modules(package: str = "docgen.documents") -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    pkg = importlib.import_module(package)
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{package}.{mod.name}")


def get(name: str):
    """'invoice.northwind' -> document instance bound to that source."""
    load_document_modules()
    try:
        cls, source = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown document variant {name!r}; known: {sorted(_REGISTRY)}") from None
    return cls(source)


def variants(doc_type: str = "*", sources: list[str] | None = None) -> list[str]:
    load_document_modules()
    return sorted(n for n, (cls, src) in _REGISTRY.items()
                  if (doc_type in ("*", cls.doc_type)) and (not sources or src in sources))


def doc_types() -> dict[str, list[str]]:
    load_document_modules()
    out: dict[str, list[str]] = {}
    for name, (cls, src) in sorted(_REGISTRY.items()):
        out.setdefault(cls.doc_type, []).append(src)
    return out
