"""目录名词确定性匹配测试 — 中文名词 → 目录真实 code。

覆盖 brief 规则表全部匹配顺序:
① semantic == code 唯一命中 → 存量透传
② name 精确唯一 → 直选;多个精确 → pending(命中集)
③ 无精确但包含唯一(semantic in name or name in semantic) → 直选;多个 → pending(命中集)
④ 零命中 → pending(全目录)
另含 lookup_selected 精确反查与 entries_from_rows 行剔除规则。
"""
from sdk.catalog import (
    CatalogEntry,
    entries_from_rows,
    lookup_selected,
    resolve_names,
)


class TestResolveNames:
    def test_exact_unique_wins_over_contains(self):
        catalog = [CatalogEntry("FIN", "财务"), CatalogEntry("FIN_MGR", "财务主管")]
        assert resolve_names(["财务"], catalog).resolved == ("FIN",)

    def test_contains_unique_resolves(self):
        catalog = [CatalogEntry("FIN", "集团财务部"), CatalogEntry("HR", "人事部")]
        assert resolve_names(["财务"], catalog).resolved == ("FIN",)

    def test_multiple_match_returns_only_matching_candidates(self):
        catalog = [CatalogEntry("A", "财务主管"), CatalogEntry("B", "销售主管")]
        result = resolve_names(["主管"], catalog)
        assert result.resolved == ()
        assert [x.code for x in result.pending[0].candidates] == ["A", "B"]

    def test_zero_match_returns_full_catalog(self):
        catalog = [CatalogEntry("FIN", "财务"), CatalogEntry("HR", "人事")]
        assert [x.code for x in resolve_names(["不存在"], catalog).pending[0].candidates] == ["FIN", "HR"]

    def test_existing_code_passes_through(self):
        result = resolve_names(["FIN"], [CatalogEntry("FIN", "财务")])
        assert result.resolved == ("FIN",) and result.pending == ()

    def test_dedupe_preserves_order(self):
        assert resolve_names(["财务", "FIN", "财务"], [CatalogEntry("FIN", "财务")]).resolved == ("FIN",)

    def test_contains_reverse_direction_resolves(self):
        # name in semantic 方向的包含唯一也直选
        catalog = [CatalogEntry("FIN", "财务"), CatalogEntry("HR", "人事")]
        assert resolve_names(["财务部门"], catalog).resolved == ("FIN",)

    def test_multiple_exact_name_returns_pending_with_exact_hits(self):
        catalog = [CatalogEntry("A", "财务"), CatalogEntry("B", "财务"), CatalogEntry("C", "销售")]
        result = resolve_names(["财务"], catalog)
        assert result.resolved == ()
        assert [x.code for x in result.pending[0].candidates] == ["A", "B"]

    def test_pending_keeps_original_semantic(self):
        result = resolve_names(["不存在"], [CatalogEntry("FIN", "财务")])
        assert result.pending[0].semantic == "不存在"


class TestLookupSelected:
    def test_found_by_name(self):
        catalog = [CatalogEntry("FIN", "财务"), CatalogEntry("HR", "人事")]
        assert lookup_selected("财务", catalog) == CatalogEntry("FIN", "财务")

    def test_found_by_code(self):
        catalog = [CatalogEntry("FIN", "财务"), CatalogEntry("HR", "人事")]
        assert lookup_selected("FIN", catalog) == CatalogEntry("FIN", "财务")

    def test_not_found_returns_none(self):
        catalog = [CatalogEntry("FIN", "财务")]
        assert lookup_selected("财务主管", catalog) is None

    def test_empty_candidates_returns_none(self):
        assert lookup_selected("财务", []) is None


class TestEntriesFromRows:
    def test_entries_from_rows_drops_incomplete_rows(self):
        entries = entries_from_rows([{"code": "FIN", "name": "财务"}, {"code": None, "name": "x"}])
        assert [e.code for e in entries] == ["FIN"]

    def test_entries_from_rows_drops_non_string_rows(self):
        entries = entries_from_rows(
            [
                {"code": "FIN", "name": "财务"},
                {"code": 1, "name": "数字code"},
                {"code": "NO_NAME"},
                {"code": "BAD", "name": 2.5},
            ]
        )
        assert [e.code for e in entries] == ["FIN"]
