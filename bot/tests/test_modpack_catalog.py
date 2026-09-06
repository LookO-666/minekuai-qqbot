"""Pure catalog normalization, independent of NoneBot and network access."""
from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "minekuai"))
catalog = importlib.import_module("modpack_catalog")


def row(**changes):
    return {
        "id": "version-1", "primaryId": "project-1", "name": "Example pack",
        "modpackVersion": "1.2.3", "gameVersion": "1.21.1", "javaVersion": 21,
        "fileName": "https://cdn.example.test/[release]&name=pack.zip", **changes,
    }


def test_normalized_item_is_immutable_and_install_parameters_are_unchanged():
    raw = row()
    item = catalog.normalize_item(raw)
    assert item == catalog.CatalogItem(
        project_id="project-1", item_id="version-1", name="Example pack", version="1.2.3",
        game_version="1.21.1", java_version="21", file_name=raw["fileName"],
    )
    assert item.installable
    raw["fileName"] = "replacement.zip"
    assert item.file_name != raw["fileName"]
    with pytest.raises(FrozenInstanceError):
        item.file_name = "changed.zip"


@pytest.mark.parametrize("primary_id", [None, "", 0])
def test_project_id_falls_back_to_item_id(primary_id):
    assert catalog.normalize_item(row(id=123, primaryId=primary_id)).project_id == "123"


@pytest.mark.parametrize("file_name", [None, "", "  "])
def test_no_file_is_browseable_but_not_installable(file_name):
    item = catalog.normalize_item(row(fileName=file_name))
    assert not item.installable


def test_display_fields_remove_controls_and_escape_cq_without_touching_file_name():
    item = catalog.normalize_item(row(
        name="\x00\u202e[CQ:at,qq=all]&\n", modpackVersion="[CQ:image,file=x]",
        gameVersion="1.21\r\n", javaVersion="21\t",
    ))
    assert item.name == "&#91;CQ:at,qq=all&#93;&amp;"
    assert "[" not in item.version and "]" not in item.version
    assert item.game_version == "1.21" and item.java_version == "21"
    assert item.file_name == row()["fileName"]


def test_all_display_fields_have_bounded_lengths():
    item = catalog.normalize_item(row(
        name="[" * 1000, modpackVersion="x" * 1000,
        gameVersion="x" * 1000, javaVersion="x" * 1000,
    ))
    assert len(item.name) <= 160
    assert len(item.version) == 80
    assert len(item.game_version) == 64
    assert len(item.java_version) == 32


@pytest.mark.parametrize("change", [
    {"id": None}, {"id": True}, {"id": 0}, {"id": -1}, {"id": "../invalid"},
    {"id": "x" * 129}, {"primaryId": {}}, {"primaryId": "[CQ:at,qq=all]"},
    {"fileName": "x" * 2049}, {"fileName": "file\n.zip"},
    {"fileName": "file\u202e.zip"}, {"fileName": ["file.zip"]}, {"name": {}},
])
def test_malformed_rows_are_rejected_without_echoing_payload(change):
    with pytest.raises(catalog.CatalogError):
        catalog.normalize_item(row(**change))


def test_parse_catalog_reads_only_root_rows_and_total():
    items, total = catalog.parse_catalog({"code": 200, "rows": [row()], "total": 200})
    assert isinstance(items, tuple) and len(items) == 1
    assert items[0].item_id == "version-1" and total == 200
    assert catalog.parse_catalog({"rows": [], "total": 0}) == ((), 0)


@pytest.mark.parametrize("payload", [
    None, [], {}, {"data": {"rows": [], "total": 0}},
    {"rows": {}, "total": 0}, {"rows": [], "total": True},
    {"rows": [], "total": "0"}, {"rows": [], "total": -1},
    {"rows": [row()], "total": 0}, {"rows": [row()] * 51, "total": 100},
    {"rows": ["not an object"], "total": 1},
])
def test_invalid_root_structure_is_rejected(payload):
    with pytest.raises(catalog.CatalogError):
        catalog.parse_catalog(payload)
