import unittest
import sys
import os
import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture(scope="session")
def credentials():
    """Fixture to load credentials from config.yaml"""
    return load_config()
