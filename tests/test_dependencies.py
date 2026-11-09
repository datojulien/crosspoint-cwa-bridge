"""Keep hashed installations aligned with runtime and build requirements."""

from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DependencyLockTests(unittest.TestCase):
    def test_declared_dependencies_match_lock(self):
        with (ROOT / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)
        locked = {
            name.lower().replace("_", "-"): version
            for name, version in re.findall(
                r"^([\w.-]+)==([^\s\\]+)",
                (ROOT / "requirements.lock").read_text(),
                re.MULTILINE,
            )
        }
        requirements = (
            project["project"]["dependencies"] + project["build-system"]["requires"]
        )
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                name, version = requirement.split("==")
                self.assertEqual(locked.get(name.lower().replace("_", "-")), version)
