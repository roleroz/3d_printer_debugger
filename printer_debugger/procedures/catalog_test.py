"""Tests for the catalog: every document loads, scope matches the schema, bad docs are rejected."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.procedures import catalog
from printer_debugger.procedures.catalog import CatalogError, load_catalog
from printer_debugger.store.models import Procedure

_VALID = """\
id: pid_tune
name: PID
scope: printer
purpose: x
test_source: null
"""


class CatalogTest(unittest.TestCase):
    """The shipped catalog is complete and consistent with the schema."""

    def setUp(self) -> None:
        self.catalog = load_catalog()

    def test_all_six_load(self) -> None:
        """Every procedure in the schema has a document."""
        self.assertEqual(set(self.catalog), {p.value for p in Procedure})

    def test_macro_procedures_have_no_test_source(self) -> None:
        """Macro-based procedures declare no test_source."""
        self.assertIsNone(self.catalog["input_shaper"].test_source)
        self.assertIsNone(self.catalog["pid_tune"].test_source)

    def test_filament_procedures_have_a_test_source(self) -> None:
        """Procedures needing a printed test declare a test_source that is not a shipped model."""
        for pid in ("temperature", "pressure_advance_flow", "stringing_retraction", "first_layer"):
            source = self.catalog[pid].test_source
            self.assertIsNotNone(source)
            self.assertNotEqual(source.get("kind"), "shipped_model")

    def test_scope_matches_schema(self) -> None:
        """Declared scope matches what the procedure_result CHECK will accept."""
        self.assertEqual(self.catalog["input_shaper"].scope, "printer")
        self.assertEqual(self.catalog["temperature"].scope, "printer_and_filament")


class CatalogValidationTest(unittest.TestCase):
    """Malformed or inconsistent documents are rejected at load."""

    def _dir_with(self, name: str, content: str) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / name).write_text(content, encoding="utf-8")
        return Path(tmp.name)

    def test_wrong_scope_rejected(self) -> None:
        """A procedure declared with the wrong scope for its id is rejected."""
        bad = _VALID.replace("scope: printer", "scope: printer_and_filament")
        with self.assertRaises(CatalogError):
            load_catalog(self._dir_with("pid_tune.yaml", bad))

    def test_shipped_model_rejected(self) -> None:
        """A test_source that ships a model is rejected — models are referenced, not shipped."""
        doc = (
            "id: temperature\nname: T\nscope: printer_and_filament\n"
            "test_source:\n  kind: shipped_model\n"
        )
        with self.assertRaises(CatalogError):
            load_catalog(self._dir_with("temperature.yaml", doc))

    def test_unknown_id_rejected(self) -> None:
        """A document with an id the schema does not know is rejected."""
        doc = "id: not_a_procedure\nname: X\nscope: printer\ntest_source: null\n"
        with self.assertRaises(CatalogError):
            load_catalog(self._dir_with("x.yaml", doc))

    def test_incomplete_catalog_rejected(self) -> None:
        """A directory missing procedures fails completeness validation."""
        with self.assertRaises(CatalogError):
            load_catalog(self._dir_with("pid_tune.yaml", _VALID))


if __name__ == "__main__":
    unittest.main()
