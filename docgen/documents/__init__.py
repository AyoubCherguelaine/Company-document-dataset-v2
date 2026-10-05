"""Concrete document types. Every module here is imported automatically by the registry.

To add a type: create <type>.py with a @register'ed BaseDocument subclass and, optionally,
templates/<type>/document.html.j2 (or <layout>.html.j2) that extends a shared layout.
"""

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates"
