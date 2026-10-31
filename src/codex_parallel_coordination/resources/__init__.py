"""Resource component 1.1.1. Windows stdlib; resource schema2, snapshot schema1."""
from .resources import Controller
from .cleanup import ArtifactCleanup

__version__ = '1.1.1'
__all__ = ['Controller', 'ArtifactCleanup', '__version__']
