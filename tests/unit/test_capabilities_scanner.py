"""The read-only Lua scanner: what it extracts, and everything it must not do."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.capabilities.scanner import (
    LUA_SUBPATH,
    MAX_PROBLEMS,
    MAX_TRUNCATION_REASONS,
    ScanError,
    ScanLimits,
    SymbolIndex,
    SymbolKind,
    WriteRefused,
    extract_symbols,
    is_inside,
    lua_root,
    normalise_symbol,
    refuse_write,
    scan_lua_tree,
)
from tests.fixtures.capability_trees import (
    VANILLA_SOURCES,
    full_lua_tree,
    source_lines,
    tree_fingerprint,
    write_lua_tree,
)

SHA = hashlib.sha256(b"x").hexdigest()


def extract(text: str) -> dict[str, SymbolKind]:
    records = extract_symbols(text, relative_path="a/b.lua", file_sha256=SHA)
    return {record.symbol: record.kind for record in records}


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_extracts_method_declarations() -> None:
    records = extract_symbols(
        "function ISEatFoodAction:new(character, item, percentage)\nend\n",
        relative_path="client/ISEatFoodAction.lua",
        file_sha256=SHA,
    )
    assert len(records) == 1
    record = records[0]
    assert record.symbol == "ISEatFoodAction:new"
    assert record.kind is SymbolKind.METHOD
    assert record.params == ("character", "item", "percentage")
    assert record.signature == "ISEatFoodAction:new(character, item, percentage)"
    assert record.line == 1
    assert record.key == "ISEatFoodAction.new"


def test_extracts_dot_functions_nested_owners_and_plain_globals() -> None:
    found = extract(
        "function ISTimedActionQueue.add(action)\nend\n"
        "function ISUI.Panels.build(x, y)\nend\n"
        "function globalHelper(a)\nend\n"
    )
    assert found == {
        "ISTimedActionQueue.add": SymbolKind.FUNCTION,
        "ISUI.Panels.build": SymbolKind.FUNCTION,
        "globalHelper": SymbolKind.FUNCTION,
    }


def test_extracts_table_declarations_in_both_guard_forms() -> None:
    found = extract("ISBaseTimedAction = ISBaseTimedAction or {}\nluautils = {}\n")
    assert found == {
        "ISBaseTimedAction": SymbolKind.TABLE,
        "luautils": SymbolKind.TABLE,
    }


def test_a_table_aliasing_another_table_is_not_recorded_as_a_declaration() -> None:
    # ``X = Y or {}`` declares nothing about X's own contents.
    assert extract("ISAlias = ISOther or {}\n") == {}


def test_extracts_derive_class_declarations() -> None:
    records = extract_symbols(
        'ISEatFoodAction = ISBaseTimedAction:derive("ISEatFoodAction")\n',
        relative_path="a.lua",
        file_sha256=SHA,
    )
    assert records[0].kind is SymbolKind.CLASS
    assert records[0].signature == "ISEatFoodAction <- ISBaseTimedAction"


def test_extracts_functions_assigned_to_a_table_field() -> None:
    found = extract("luautils.walkAdj = function(character, square)\nend\n")
    assert found == {"luautils.walkAdj": SymbolKind.FUNCTION}


def test_ignores_locals_line_comments_and_block_comments() -> None:
    found = extract(
        "local function privateHelper(x)\nend\n"
        "local ISPrivate = {}\n"
        "-- function ISCommented:new(x)\n"
        "--[[\nfunction ISGhost:new(x)\n]]\n"
        "--[==[\nfunction ISDeepGhost:new(x)\n]==]\n"
    )
    assert found == {}


def test_a_block_comment_that_opens_and_closes_on_one_line_does_not_swallow_the_file() -> None:
    found = extract("--[[ inline ]]\nfunction ISReal:new(x)\nend\n")
    assert found == {"ISReal:new": SymbolKind.METHOD}


def test_a_line_comment_mentioning_block_syntax_does_not_swallow_the_file() -> None:
    # ``--[[`` inside a line comment is prose, not an opener. Treating it as one
    # would drop every declaration up to the next ``]]`` — silently, and the
    # capability behind them would report as missing.
    found = extract(
        "-- see the note --[[ in the design doc\n"
        "function ISReal:new(x)\nend\n"
        "function ISAlsoReal.helper(y)\nend\n"
    )
    assert found == {
        "ISReal:new": SymbolKind.METHOD,
        "ISAlsoReal.helper": SymbolKind.FUNCTION,
    }


def test_symbol_extraction_is_bounded_by_max_symbols() -> None:
    text = "".join(f"function Thing.fn{i}(a)\nend\n" for i in range(50))
    records = extract_symbols(text, relative_path="a.lua", file_sha256=SHA, max_symbols=5)
    assert len(records) == 5


def test_normalise_symbol_folds_the_method_colon() -> None:
    assert normalise_symbol("ISEatFoodAction:new") == "ISEatFoodAction.new"
    assert normalise_symbol("ISEatFoodAction.new") == "ISEatFoodAction.new"


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def test_index_finds_a_colon_declaration_by_its_dotted_probe_name(tmp_path: Path) -> None:
    index = scan_lua_tree(write_lua_tree(tmp_path)).index()
    assert index.has("ISEatFoodAction.new")
    assert index.has("ISEatFoodAction:new")
    record = index.find("ISEatFoodAction.new")
    assert record is not None
    assert record.relative_path == "client/TimedActions/ISEatFoodAction.lua"


def test_index_reports_missing_symbols_without_raising() -> None:
    index = SymbolIndex.from_records(())
    assert index.find("Nothing.here") is None
    assert not index.has("Nothing.here")
    assert index.missing(["A.b", "C.d"]) == ("A.b", "C.d")


# ---------------------------------------------------------------------------
# scanning a tree
# ---------------------------------------------------------------------------


def test_scan_records_the_file_hash_of_each_symbols_source_file(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    result = scan_lua_tree(root)
    record = result.index().find("ISEatFoodAction.new")
    assert record is not None
    expected = hashlib.sha256(
        (root / "client" / "TimedActions" / "ISEatFoodAction.lua").read_bytes()
    ).hexdigest()
    assert record.file_sha256 == expected


def test_scan_ignores_non_lua_files_and_reports_what_it_saw(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    result = scan_lua_tree(root)
    assert result.files_scanned == len([k for k in VANILLA_SOURCES if k.endswith(".lua")])
    assert not result.truncated
    assert result.problems == ()
    assert all(record.relative_path.endswith(".lua") for record in result.symbols)


def test_the_report_carries_no_line_of_the_scanned_source(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    serialised = json.dumps(scan_lua_tree(root).to_dict())
    leaked = [line for line in source_lines() if line in serialised]
    assert leaked == [], f"scanner leaked game source into the report: {leaked}"


def test_the_report_records_relative_paths_only(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path / "Пользователь" / "ProjectZomboid")
    serialised = json.dumps(scan_lua_tree(root).to_dict(), ensure_ascii=False)
    assert "Пользователь" not in serialised
    assert all(not record.relative_path.startswith("/") for record in scan_lua_tree(root).symbols)


def test_scanning_neither_writes_nor_copies_anything(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    root = write_lua_tree(game_dir)
    before = tree_fingerprint(game_dir)
    outside_before = tree_fingerprint(tmp_path)

    scan_lua_tree(root)

    assert tree_fingerprint(game_dir) == before
    # Nothing new anywhere: a scanner that copied files into a cache would show
    # up here as an extra entry.
    assert tree_fingerprint(tmp_path) == outside_before


def test_scanning_works_against_a_read_only_game_directory(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    root = write_lua_tree(game_dir)
    read_only = stat.S_IRUSR | stat.S_IXUSR
    directories = [p for p in game_dir.rglob("*") if p.is_dir()] + [game_dir]
    for directory in directories:
        directory.chmod(read_only)
    try:
        result = scan_lua_tree(root)
        assert result.symbols
    finally:
        for directory in directories:
            directory.chmod(stat.S_IRWXU)


def test_refuse_write_blocks_any_path_inside_the_game_directory(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    root = write_lua_tree(game_dir)
    for destination in (
        game_dir / "report.json",
        root / "compat" / "generated_api_report.json",
        game_dir,
    ):
        with pytest.raises(WriteRefused, match="refusing to write inside the game directory"):
            refuse_write(destination, [game_dir])
    assert not (game_dir / "report.json").exists()


def test_refuse_write_allows_a_sibling_directory(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    destination = tmp_path / "state" / "generated_api_report.json"
    assert refuse_write(destination, [game_dir]) == destination
    # A path whose name merely starts with the protected one is not inside it.
    assert not is_inside(tmp_path / "game-backup" / "x.json", game_dir)


def test_scan_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir.lua"
    file_path.write_text("function X:y()\nend\n", encoding="utf-8")
    with pytest.raises(ScanError, match="not a directory"):
        scan_lua_tree(file_path)


def test_lua_root_points_at_media_lua(tmp_path: Path) -> None:
    assert lua_root(tmp_path) == tmp_path.joinpath(*LUA_SUBPATH)


# ---------------------------------------------------------------------------
# bounds — every one of them is reported, never applied silently
# ---------------------------------------------------------------------------


def test_a_file_cap_truncates_the_scan_and_says_so(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    result = scan_lua_tree(root, ScanLimits(max_files=2))
    assert result.files_seen == 2
    assert result.truncated
    assert any("stopped after 2 files" in reason for reason in result.truncation_reasons)


def test_an_oversized_file_is_skipped_whole_and_reported(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    (root / "huge.lua").write_text("function Huge:new(a)\nend\n" + "-- pad\n" * 500, "utf-8")
    result = scan_lua_tree(root, ScanLimits(max_file_bytes=64))
    assert result.truncated
    assert any("over the per-file cap" in problem for problem in result.problems)
    assert not result.index().has("Huge.new")


def test_a_total_byte_cap_stops_the_scan_and_says_so(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    result = scan_lua_tree(root, ScanLimits(max_total_bytes=100))
    assert result.truncated
    assert any("bytes" in reason for reason in result.truncation_reasons)
    assert result.files_scanned < result.files_seen


def test_a_symbol_cap_stops_the_scan_and_says_so(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    result = scan_lua_tree(root, ScanLimits(max_symbols=3))
    assert len(result.symbols) == 3
    assert result.truncated
    assert any("symbol" in reason for reason in result.truncation_reasons)


def test_a_depth_cap_stops_the_descent_and_says_so(tmp_path: Path) -> None:
    root = write_lua_tree(tmp_path)
    deep = root / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "Deep.lua").write_text("function Deep:new(a)\nend\n", encoding="utf-8")
    result = scan_lua_tree(root, ScanLimits(max_depth=2))
    assert result.truncated
    assert any("depth" in reason for reason in result.truncation_reasons)
    assert not result.index().has("Deep.new")


def test_limits_must_be_positive() -> None:
    with pytest.raises(ScanError, match="max_files must be positive"):
        ScanLimits(max_files=0)


def test_a_dangling_symlink_is_reported_and_does_not_stop_the_scan(tmp_path: Path) -> None:
    root = full_lua_tree(tmp_path)
    (root / "Broken.lua").symlink_to(root / "does-not-exist.lua")
    result = scan_lua_tree(root)
    assert any("cannot stat" in problem for problem in result.problems)
    assert result.index().has("ISEatFoodAction.new")


def test_the_problem_list_is_bounded_and_says_that_it_was_cut(tmp_path: Path) -> None:
    root = full_lua_tree(tmp_path)
    for i in range(MAX_PROBLEMS * 2):
        (root / f"Broken{i:03d}.lua").symlink_to(root / f"missing{i:03d}.lua")
    result = scan_lua_tree(root)
    assert len(result.problems) == MAX_PROBLEMS
    assert result.problems[-1].startswith(f"more than {MAX_PROBLEMS - 1}")
    # The scan still finished: a flood of broken entries must not cost the index.
    assert result.index().has("ISEatFoodAction.new")


def test_one_deep_subtree_does_not_add_one_truncation_reason_per_directory(
    tmp_path: Path,
) -> None:
    root = write_lua_tree(tmp_path)
    for i in range(MAX_TRUNCATION_REASONS * 2):
        deep = root / f"branch{i:03d}" / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "Deep.lua").write_text("function Deep:new(a)\nend\n", encoding="utf-8")
    result = scan_lua_tree(root, ScanLimits(max_depth=2))
    assert len(result.truncation_reasons) <= MAX_TRUNCATION_REASONS
    assert result.truncation_reasons == ("stopped descending below depth 2",)
    assert not result.index().has("Deep.new")


def test_a_file_that_grows_past_the_cap_after_the_stat_is_refused_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hashing a partial read would record a sha256 that describes no file on
    # disk, and a signature reconstructed from half a file.
    root = write_lua_tree(tmp_path)
    grown = root / "Grown.lua"
    grown.write_text("function Grown:new(a)\nend\n" + "-- pad\n" * 200, encoding="utf-8")
    real_stat = Path.stat

    def understating_stat(self: Path, **kwargs: object) -> os.stat_result:
        result = real_stat(self, **kwargs)  # type: ignore[arg-type]
        if self.name == "Grown.lua":
            fields = list(result)
            fields[stat.ST_SIZE] = 8
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", understating_stat)
    result = scan_lua_tree(root, ScanLimits(max_file_bytes=600))
    assert not result.index().has("Grown.new")
    assert any("grew past the per-file cap" in problem for problem in result.problems)
    assert result.truncated
    # The rest of the tree was still indexed.
    assert result.index().has("ISEatFoodAction.new")


def test_an_unreadable_file_is_reported_and_does_not_stop_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file the scanner cannot open must not cost it the other findings.

    The refusal is injected rather than produced with ``chmod``. ``chmod(0)``
    does not deny a read on Windows and ``os.geteuid`` does not exist there, so
    the previous form of this test could not run on the platform this project
    ships to — and the behaviour under test is the *handling* of an ``OSError``,
    which no filesystem needs to be involved to provoke.
    """
    root = full_lua_tree(tmp_path)
    blocked = root / "client" / "TimedActions" / "ISReadABook.lua"
    real_open = Path.open

    def refusing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == blocked:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refusing)

    result = scan_lua_tree(root)

    assert any("cannot read" in problem for problem in result.problems)
    assert result.index().has("ISEatFoodAction.new"), "one refused file stopped the whole scan"
