"""
Check script infrastructure for SDLC pipeline.

This package provides the base classes and utilities for implementing
check scripts that validate code during each pipeline phase.
"""

from .base import CheckRunner

__all__ = ["CheckRunner"]
