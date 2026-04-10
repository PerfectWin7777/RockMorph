# base/base_engine.py

from abc import ABC, abstractmethod


class BaseEngine(ABC):
    """
    Abstract base class for all RockMorph computation engines.
    Each tool must implement compute() and optionally override validate().
    """

    @abstractmethod
    def compute(self, **kwargs) -> dict:
        """
        Run the computation.
        Always receives named parameters.
        Always returns a pure data dict — never any UI logic here.
        """
        pass

    def validate(self, **kwargs) -> bool:
        """
        Basic input validation before compute().
        Override in subclasses for tool-specific checks.
        """
        return True