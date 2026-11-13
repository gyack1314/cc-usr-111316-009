"""精确有理数线性规划内核（两阶段单纯形，Bland 规则）。

所有算术使用 :class:`fractions.Fraction`。返回的解附带可核对证书：
原始可行、对偶可行（全部结构变量约化费用不小于零），目标值由可行基
直接给出。求解器不依赖任何第三方库，也不使用浮点数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Sequence


OPTIMAL = "optimal"
INFEASIBLE = "infeasible"
UNBOUNDED = "unbounded"
STEP_LIMIT = "step_limit"


@dataclass
class LPResult:
    status: str
    objective: Fraction | None = None
    vector: list[Fraction] | None = None
    certificate: dict = field(default_factory=dict)
    pivots: int = 0


def _dot(a: Sequence[Fraction], b: Sequence[Fraction]) -> Fraction:
    total = Fraction(0)
    for x, y in zip(a, b):
        total += x * y
    return total


class _Tableau:
    def __init__(self, rows: list[list[Fraction]], rhs: list[Fraction], basis: list[int]):
        self.rows = rows
        self.rhs = rhs
        self.basis = basis
        self.pivots = 0

    @property
    def m(self) -> int:
        return len(self.rows)

    def reduced_costs(self, c: Sequence[Fraction]) -> list[Fraction]:
        cb = [c[j] for j in self.basis]
        r = list(c)
        for i, row in enumerate(self.rows):
            factor = cb[i]
            if factor:
                for j, value in enumerate(row):
                    if value:
                        r[j] -= factor * value
        for j in self.basis:
            r[j] = Fraction(0)
        return r

    def objective(self, c: Sequence[Fraction]) -> Fraction:
        return _dot([c[j] for j in self.basis], self.rhs)

    def pivot(self, row_index: int, column: int) -> None:
        pivot = self.rows[row_index][column]
        if pivot == 0:
            raise AssertionError("主元为零")
        self.rows[row_index] = [v / pivot for v in self.rows[row_index]]
        self.rhs[row_index] /= pivot
        for i, row in enumerate(self.rows):
            if i == row_index:
                continue
            factor = row[column]
            if factor:
                self.rows[i] = [v - factor * w for v, w in zip(row, self.rows[row_index])]
                self.rhs[i] -= factor * self.rhs[row_index]
        self.basis[row_index] = column

    def simplex(self, c: Sequence[Fraction], limit: int) -> str:
        """最小化 c。Bland 规则保证有限终止。"""
        while True:
            if self.pivots >= limit:
                return STEP_LIMIT
            r = self.reduced_costs(c)
            entering = next((j for j, value in enumerate(r) if value < 0), None)
            if entering is None:
                return OPTIMAL
            ratio_row = None
            best = None
            for i, row in enumerate(self.rows):
                a = row[entering]
                if a > 0:
                    ratio = self.rhs[i] / a
                    if best is None or ratio < best or (ratio == best and self.basis[i] < self.basis[ratio_row]):
                        best = ratio
                        ratio_row = i
            if ratio_row is None:
                return UNBOUNDED
            self.pivot(ratio_row, entering)
            self.pivots += 1


def solve_lp(
    c: Sequence[Fraction],
    a_eq: Sequence[Sequence[Fraction]] = (),
    b_eq: Sequence[Fraction] = (),
    a_le: Sequence[Sequence[Fraction]] = (),
    b_le: Sequence[Fraction] = (),
    pivot_limit: int = 20000,
) -> LPResult:
    """最小化 c x，满足 A_eq x = b_eq、A_le x <= b_le、x >= 0。

    返回的结构变量向量长度为 ``len(c)``。``certificate`` 中的
    ``primal_feasible`` / ``dual_feasible`` 来自最终的基直接核对。
    """
    n = len(c)
    if len(a_eq) != len(b_eq) or len(a_le) != len(b_le):
        raise ValueError("约束行数与右端项数量不一致")
    rows: list[list[Fraction]] = []
    rhs: list[Fraction] = []
    types: list[str] = []
    for source, right in ((a_eq, b_eq), (a_le, b_le)):
        for row, value in zip(source, right):
            if len(row) != n:
                raise ValueError("约束维度与目标向量长度不一致")
            coeffs = [Fraction(v) for v in row]
            bound = Fraction(value)
            if bound < 0:
                coeffs = [-v for v in coeffs]
                bound = -bound
                if source is a_eq:
                    types.append("=")
                else:
                    types.append(">=")
            else:
                types.append("=" if source is a_eq else "<=")
            rows.append(coeffs)
            rhs.append(bound)

    n_slack = len(rows)
    n_art = sum(1 for t in types if t != "<=")
    total = n + n_slack + n_art
    matrix: list[list[Fraction]] = []
    basis: list[int] = []
    art_cols: list[int] = []
    art_index = n + n_slack
    for i, (row, t) in enumerate(zip(rows, types)):
        coeffs = row + [Fraction(0)] * (n_slack + n_art)
        if t == "<=":
            coeffs[n + i] = Fraction(1)
            basis.append(n + i)
        else:
            if t == ">=":
                coeffs[n + i] = Fraction(-1)
            coeffs[art_index] = Fraction(1)
            art_cols.append(art_index)
            basis.append(art_index)
            art_index += 1
        matrix.append(coeffs)

    table = _Tableau(matrix, rhs, basis)
    if art_cols:
        c1 = [Fraction(0)] * total
        for j in art_cols:
            c1[j] = Fraction(1)
        status = table.simplex(c1, pivot_limit)
        if status == STEP_LIMIT:
            return LPResult(STEP_LIMIT, pivots=table.pivots)
        if status == UNBOUNDED:
            raise AssertionError("第一阶段不应无界")
        if table.objective(c1) > 0:
            return LPResult(INFEASIBLE, pivots=table.pivots)

    # 把仍留在基上的人工变量（必为零）换出；若整行冗余则删除。
    drop_rows: list[int] = []
    for i in range(table.m):
        if table.basis[i] in art_cols:
            if table.rhs[i] != 0:
                raise AssertionError("人工变量在基上但取值非零")
            replacement = next(
                (j for j in range(total) if j not in art_cols and table.rows[i][j] != 0),
                None,
            )
            if replacement is None:
                drop_rows.append(i)
            else:
                table.pivot(i, replacement)

    keep_rows = [i for i in range(table.m) if i not in drop_rows]
    keep_cols = [j for j in range(total) if j not in art_cols]
    col_map = {j: k for k, j in enumerate(keep_cols)}
    matrix2 = [[table.rows[i][j] for j in keep_cols] for i in keep_rows]
    rhs2 = [table.rhs[i] for i in keep_rows]
    basis2 = [col_map[table.basis[i]] for i in keep_rows]
    table2 = _Tableau(matrix2, rhs2, basis2)
    table2.pivots = table.pivots

    c2 = [Fraction(0)] * len(keep_cols)
    for j in range(n):
        c2[col_map[j]] = Fraction(c[j])
    status2 = table2.simplex(c2, pivot_limit)
    if status2 == STEP_LIMIT:
        return LPResult(STEP_LIMIT, pivots=table2.pivots)
    if status2 == UNBOUNDED:
        return LPResult(UNBOUNDED, pivots=table2.pivots)

    x = [Fraction(0)] * len(keep_cols)
    for i, j in enumerate(table2.basis):
        x[j] = table2.rhs[i]
    objective = table2.objective(c2)
    reduced = table2.reduced_costs(c2)
    primal_feasible = all(v >= 0 for v in x)
    for row, right in zip(matrix2, rhs2):
        if _dot(row, x) != right:
            primal_feasible = False
            break
    dual_feasible = all(v >= 0 for v in reduced)
    if not (primal_feasible and dual_feasible):
        raise AssertionError("LP 证书核对失败")
    vector = [x[col_map[j]] for j in range(n)]
    certificate = {
        "primal_feasible": primal_feasible,
        "dual_feasible": dual_feasible,
        "min_reduced_cost": min(reduced) if reduced else Fraction(0),
        "basis_size": len(table2.basis),
    }
    return LPResult(OPTIMAL, objective, vector, certificate, table2.pivots)
