"""Tokenizer and recursive-descent parser for Quarry's SQL.

The grammar follows SQLite's. Operator precedence, from loosest to tightest::

    OR
    AND
    NOT
    =  ==  !=  <>  IS  IS NOT  IN  LIKE  GLOB  BETWEEN  ISNULL  NOTNULL
    <  <=  >  >=
    &  |  <<  >>
    +  -
    *  /  %
    ||
    unary -  +  ~        and postfix COLLATE
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import NotSupported, SQLError
from . import ast as A

KEYWORDS = {
    "ABORT", "ALL", "ALTER", "ANALYZE", "AND", "AS", "ASC", "AUTOINCREMENT", "BEGIN", "BETWEEN", "BY", "CASE",
    "CAST", "CHECK", "COLLATE", "COLUMN", "COMMIT", "CONFLICT", "CONSTRAINT", "CREATE", "CROSS", "CURRENT",
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "DEFAULT", "DEFERRED", "DELETE", "DESC", "DISTINCT", "DO",
    "DROP", "ELSE", "END", "ESCAPE", "EXCEPT", "EXCLUSIVE", "EXISTS", "EXPLAIN", "FAIL", "FILTER", "FOLLOWING",
    "FOREIGN", "FROM", "FULL", "GLOB", "GROUP", "HAVING", "IF", "IGNORE", "IMMEDIATE", "IN", "INDEX", "INNER",
    "INSERT", "INTERSECT", "INTO", "IS", "ISNULL", "JOIN", "KEY", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT",
    "NOTHING", "NOTNULL", "NULL", "NULLS", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER", "PARTITION", "PLAN",
    "PRAGMA", "PRECEDING", "PRIMARY", "QUERY", "RANGE", "RECURSIVE", "REFERENCES", "RELEASE", "RENAME", "REPLACE",
    "RETURNING", "RIGHT", "ROLLBACK", "ROW", "ROWS", "SAVEPOINT", "SELECT", "SET", "TABLE", "TEMP", "TEMPORARY",
    "THEN", "TO", "TRANSACTION", "UNBOUNDED", "UNION", "UNIQUE", "UPDATE", "USING", "VACUUM", "VALUES", "VIEW",
    "WHEN", "WHERE", "WINDOW", "WITH", "WITHOUT", "FIRST", "LAST", "ADD", "EXCLUDE",
}  # fmt: skip

# words that cannot be used as bare identifiers (others, like KEY or PLAN, can)
RESERVED = {
    "ALL", "ALTER", "AND", "AS", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLLATE", "CONSTRAINT", "CREATE",
    "CROSS", "DEFAULT", "DELETE", "DISTINCT", "DROP", "ELSE", "END", "ESCAPE", "EXCEPT", "EXISTS", "FROM", "FULL",
    "GLOB", "GROUP", "HAVING", "IN", "INDEX", "INNER", "INSERT", "INTERSECT", "INTO", "IS", "ISNULL", "JOIN",
    "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NOTNULL", "NULL", "OFFSET", "ON", "OR", "ORDER", "OUTER",
    "PRIMARY", "REFERENCES", "RETURNING", "RIGHT", "SELECT", "SET", "TABLE", "THEN", "TO", "UNION", "UNIQUE",
    "UPDATE", "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH",
}  # fmt: skip


@dataclass
class Token:
    kind: str  # word, ident (quoted), string, number, blob, param, op, eof
    value: object
    pos: int
    end: int
    upper: str = ""


_TOKEN = re.compile(
    r"""
    (?P<space>\s+|--[^\n]*|/\*.*?(?:\*/|\Z))
  | (?P<blob>[xX]'(?P<hex>[0-9a-fA-F]*)')
  | (?P<number>0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<word>[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*)
  | (?P<string>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])
  | (?P<param>\?\d*|[:@$][A-Za-z_0-9]+)
  | (?P<op>\|\||<<|>>|<=|>=|==|!=|<>|->>|->|[-+*/%<>=~&|(),.;])
    """,
    re.VERBOSE | re.DOTALL,
)


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    n = len(sql)
    while pos < n:
        m = _TOKEN.match(sql, pos)
        if not m:
            ch = sql[pos]
            if ch in "'\"`[":
                raise SQLError("unterminated quoted text", sql, pos, "add the closing quote")
            raise SQLError(f"unexpected character {ch!r}", sql, pos)
        kind = m.lastgroup
        text = m.group(0)
        end = m.end()
        if kind == "space":
            if text.startswith("/*") and not text.endswith("*/"):
                raise SQLError("unterminated /* comment", sql, pos)
        elif kind == "hex":
            pass
        elif kind == "blob":
            hexdigits = m.group("hex")
            if len(hexdigits) % 2:
                raise SQLError("a blob literal needs an even number of hex digits", sql, pos)
            tokens.append(Token("blob", bytes.fromhex(hexdigits), pos, end))
        elif kind == "number":
            if text[:2].lower() == "0x":
                value: object = int(text, 16)
                if value > 2**63 - 1:
                    value -= 2**64  # type: ignore[operator]
            elif re.fullmatch(r"\d+", text):
                value = int(text)
                if value > 2**63 - 1:
                    value = float(text)
            else:
                value = float(text)
            if end < n and (sql[end].isalpha() or sql[end] == "_"):
                raise SQLError(f"unrecognized token {sql[pos : end + 1]!r}", sql, pos)
            tokens.append(Token("number", value, pos, end))
        elif kind == "word":
            tokens.append(Token("word", text, pos, end, text.upper()))
        elif kind == "string":
            tokens.append(Token("string", text[1:-1].replace("''", "'"), pos, end))
        elif kind == "qident":
            q = text[0]
            body = text[1:-1]
            if q == '"':
                body = body.replace('""', '"')
            elif q == "`":
                body = body.replace("``", "`")
            tokens.append(Token("ident", body, pos, end))
        elif kind == "param":
            tokens.append(Token("param", text, pos, end))
        else:
            tokens.append(Token("op", text, pos, end, text))
        pos = end
    tokens.append(Token("eof", None, n, n, ""))
    return tokens


class Parser:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.tokens = tokenize(sql)
        self.i = 0
        self.param_count = 0
        self.named: dict[str, int] = {}

    # ------------------------------------------------------------ helpers

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def peek(self, k: int = 1) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def advance(self) -> Token:
        t = self.tokens[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def error(self, message: str, tok: Token | None = None, hint: str = "") -> SQLError:
        t = tok or self.tok
        return SQLError(message, self.sql, t.pos, hint)

    def describe(self, t: Token) -> str:
        if t.kind == "eof":
            return "the end of the statement"
        return repr(self.sql[t.pos : t.end])

    def at(self, *words: str) -> bool:
        t = self.tok
        return (t.kind == "word" and t.upper in words) or (t.kind == "op" and t.value in words)

    def at_seq(self, *words: str) -> bool:
        for k, w in enumerate(words):
            t = self.peek(k)
            if not ((t.kind == "word" and t.upper == w) or (t.kind == "op" and t.value == w)):
                return False
        return True

    def accept(self, *words: str) -> Token | None:
        if self.at(*words):
            return self.advance()
        return None

    def expect(self, word: str, context: str = "") -> Token:
        if self.at(word):
            return self.advance()
        where = f" {context}" if context else ""
        raise self.error(f"expected {word}{where}, found {self.describe(self.tok)}")

    def ident(self, what: str = "a name") -> str:
        t = self.tok
        if t.kind == "ident":
            self.advance()
            return str(t.value)
        if t.kind == "word" and t.upper not in RESERVED:
            self.advance()
            return str(t.value)
        if t.kind == "string":  # SQLite accepts 'name' where a name is expected
            self.advance()
            return str(t.value)
        hint = ""
        if t.kind == "word":
            hint = f'{t.value} is a reserved word: write "{t.value}" in double quotes to use it as a name'
        raise self.error(f"expected {what}, found {self.describe(t)}", hint=hint)

    def is_ident(self, t: Token | None = None) -> bool:
        t = t or self.tok
        return t.kind == "ident" or (t.kind == "word" and t.upper not in RESERVED)

    def span(self, start: int) -> str:
        end = self.tokens[self.i - 1].end if self.i > 0 else start
        return self.sql[start:end]

    def mark(self, node, start: int):  # type: ignore[no-untyped-def]
        node.pos = start
        node.text = self.span(start)
        return node

    # ------------------------------------------------------------ statements

    def parse_script(self) -> list[A.Node]:
        statements = []
        while True:
            while self.accept(";"):
                pass
            if self.tok.kind == "eof":
                return statements
            statements.append(self.statement())
            if self.tok.kind != "eof" and not self.at(";"):
                raise self.error(f"expected ; or the end of the statement, found {self.describe(self.tok)}")

    def statement(self) -> A.Node:
        t = self.tok
        start = t.pos
        if t.kind != "word":
            raise self.error(f"expected a SQL statement, found {self.describe(t)}")
        kw = t.upper
        if kw == "EXPLAIN":
            self.advance()
            plan = bool(self.accept("QUERY"))
            if plan:
                self.expect("PLAN")
            return self.mark(A.Explain(self.statement(), True), start)
        if kw in ("SELECT", "VALUES", "WITH"):
            if kw == "WITH" and self._with_is_dml():
                return self.dml_with()
            return self.select()
        if kw in ("INSERT", "REPLACE"):
            return self.insert()
        if kw == "UPDATE":
            return self.update()
        if kw == "DELETE":
            return self.delete()
        if kw == "CREATE":
            return self.create()
        if kw == "DROP":
            return self.drop()
        if kw == "ALTER":
            return self.alter()
        if kw in ("BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"):
            return self.transaction()
        if kw == "PRAGMA":
            return self.pragma()
        if kw == "VACUUM":
            self.advance()
            return self.mark(A.Vacuum(), start)
        if kw in ("ATTACH", "DETACH", "REINDEX"):
            raise NotSupported(f"{kw} is not supported", self.sql, t.pos, "Quarry has one database per connection")
        if kw == "ANALYZE":
            self.advance()
            if self.is_ident():
                self.advance()
            return self.mark(A.Analyze(), start)
        raise self.error(
            f"unknown statement {t.value!r}",
            hint="Quarry runs SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, BEGIN/COMMIT/ROLLBACK, EXPLAIN and PRAGMA",
        )

    def _with_is_dml(self) -> bool:
        depth = 0
        for t in self.tokens[self.i :]:
            if t.kind == "op" and t.value == "(":
                depth += 1
            elif t.kind == "op" and t.value == ")":
                depth -= 1
            elif depth == 0 and t.kind == "word" and t.upper in ("SELECT", "VALUES"):
                return False
            elif depth == 0 and t.kind == "word" and t.upper in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
                return True
        return False

    def dml_with(self) -> A.Node:
        start = self.tok.pos
        ctes, recursive = self.with_clause()
        stmt = self.statement()
        if not isinstance(stmt, (A.Insert, A.Update, A.Delete)):
            raise self.error("WITH must be followed by SELECT, INSERT, UPDATE or DELETE")
        stmt.ctes = ctes  # type: ignore[attr-defined]
        stmt.recursive = recursive  # type: ignore[attr-defined]
        stmt.pos = start
        return stmt

    # ------------------------------------------------------------ SELECT

    def with_clause(self) -> tuple[list[A.CTE], bool]:
        self.expect("WITH")
        recursive = bool(self.accept("RECURSIVE"))
        ctes = []
        while True:
            start = self.tok.pos
            name = self.ident("a name for the common table expression")
            cols = None
            if self.accept("("):
                cols = self.name_list()
                self.expect(")")
            self.expect("AS", "after the CTE name")
            if self.accept("NOT"):
                self.expect("MATERIALIZED")
            else:
                self.accept("MATERIALIZED")
            self.expect("(", "before the CTE's query")
            sel = self.select()
            self.expect(")", "after the CTE's query")
            ctes.append(self.mark(A.CTE(name, cols, sel), start))
            if not self.accept(","):
                return ctes, recursive

    def select(self) -> A.Select:
        start = self.tok.pos
        ctes: list[A.CTE] = []
        recursive = False
        if self.at("WITH"):
            ctes, recursive = self.with_clause()
        cores: list[A.SelectCore | A.Values] = [self.select_core()]
        ops: list[str] = []
        while self.at("UNION", "INTERSECT", "EXCEPT"):
            op = self.advance().upper
            if op == "UNION" and self.accept("ALL"):
                op = "UNION ALL"
            ops.append(op)
            cores.append(self.select_core())
        sel = A.Select(cores, ops, ctes=ctes, recursive=recursive)
        if self.accept("ORDER"):
            self.expect("BY")
            sel.order_by = self.order_list()
        if self.accept("LIMIT"):
            sel.limit = self.expr()
            if self.accept("OFFSET"):
                sel.offset = self.expr()
            elif self.accept(","):
                # LIMIT offset, count
                sel.offset, sel.limit = sel.limit, self.expr()
        return self.mark(sel, start)

    def order_list(self) -> list[A.OrderItem]:
        items = []
        while True:
            start = self.tok.pos
            e = self.expr()
            desc = False
            if self.accept("DESC"):
                desc = True
            else:
                self.accept("ASC")
            nulls = None
            if self.accept("NULLS"):
                nulls = bool(self.accept("FIRST"))
                if not nulls:
                    self.expect("LAST", "after NULLS")
            items.append(self.mark(A.OrderItem(e, desc, nulls), start))
            if not self.accept(","):
                return items

    def select_core(self) -> A.SelectCore | A.Values:
        start = self.tok.pos
        if self.accept("VALUES"):
            rows = []
            while True:
                self.expect("(", "before a row of VALUES")
                rows.append(self.expr_list())
                self.expect(")", "after a row of VALUES")
                if not self.accept(","):
                    break
            widths = {len(r) for r in rows}
            if len(widths) > 1:
                raise SQLError("all VALUES rows must have the same number of values", self.sql, start)
            return self.mark(A.Values(rows), start)
        if self.at("("):
            raise self.error("a parenthesised query cannot be used here", hint="remove the parentheses, or use it in FROM as a subquery")
        self.expect("SELECT", "to start a query")
        distinct = False
        if self.accept("DISTINCT"):
            distinct = True
        else:
            self.accept("ALL")
        columns = []
        while True:
            columns.append(self.result_column())
            if not self.accept(","):
                break
        core = A.SelectCore(columns, distinct=distinct)
        if self.accept("FROM"):
            core.from_ = self.from_clause()
        if self.accept("WHERE"):
            core.where = self.expr()
        if self.accept("GROUP"):
            self.expect("BY")
            core.group_by = self.expr_list()
        if self.accept("HAVING"):
            core.having = self.expr()
        if self.accept("WINDOW"):
            while True:
                name = self.ident("a window name")
                self.expect("AS")
                core.windows[name.lower()] = self.window_spec()
                if not self.accept(","):
                    break
        return self.mark(core, start)

    def result_column(self) -> A.ResultColumn:
        start = self.tok.pos
        if self.accept("*"):
            return self.mark(A.ResultColumn(None, star=True), start)
        if self.is_ident() and self.peek().value == "." and self.peek(2).value == "*":
            table = self.ident()
            self.advance()
            self.advance()
            return self.mark(A.ResultColumn(None, star=True, star_table=table), start)
        e = self.expr()
        alias = None
        if self.accept("AS"):
            alias = self.ident("a column alias")
        elif self.is_ident() and not self.at("FROM", "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "WINDOW"):
            alias = self.ident()
        rc = A.ResultColumn(e, alias)
        rc.pos = start
        rc.text = e.text
        return rc

    def from_clause(self) -> A.FromItem:
        left = self.table_or_subquery()
        while True:
            start = self.tok.pos
            if self.accept(","):
                left = self.mark(A.Join("CROSS", left, self.table_or_subquery()), start)
                continue
            natural = bool(self.accept("NATURAL"))
            kind = None
            if self.accept("JOIN"):
                kind = "INNER"
            elif self.accept("INNER"):
                self.expect("JOIN")
                kind = "INNER"
            elif self.accept("CROSS"):
                self.expect("JOIN")
                kind = "CROSS"
            elif self.at("LEFT", "RIGHT", "FULL"):
                kind = self.advance().upper
                self.accept("OUTER")
                self.expect("JOIN")
                if kind != "LEFT":
                    raise NotSupported(f"{kind} JOIN is not supported yet", self.sql, start, "swap the tables and use LEFT JOIN")
            elif natural:
                raise self.error("expected JOIN after NATURAL")
            if kind is None:
                return left
            right = self.table_or_subquery()
            join = A.Join(kind, left, right, natural=natural)
            if self.accept("ON"):
                join.on = self.expr()
            elif self.accept("USING"):
                self.expect("(")
                join.using = self.name_list()
                self.expect(")")
            left = self.mark(join, start)

    def table_or_subquery(self) -> A.FromItem:
        start = self.tok.pos
        if self.accept("("):
            if self.at("SELECT", "VALUES", "WITH"):
                sel = self.select()
                self.expect(")", "after the subquery")
                alias = self.alias()
                return self.mark(A.SubqueryRef(sel, alias), start)
            inner = self.from_clause()
            self.expect(")")
            return inner
        name = self.ident("a table name")
        if self.accept("."):  # schema.table: only "main" exists
            if name.lower() not in ("main", "temp"):
                raise SQLError(f"unknown database {name!r}", self.sql, start, "Quarry has one database per file: drop the prefix")
            name = self.ident("a table name")
        if self.at("("):
            raise NotSupported("table-valued functions are not supported yet", self.sql, self.tok.pos)
        return self.mark(A.TableRef(name, self.alias()), start)

    def alias(self) -> str | None:
        if self.accept("AS"):
            return self.ident("an alias")
        if self.is_ident() and not self.at(
            "ON",
            "USING",
            "WHERE",
            "GROUP",
            "ORDER",
            "LIMIT",
            "JOIN",
            "LEFT",
            "INNER",
            "CROSS",
            "NATURAL",
            "HAVING",
            "WINDOW",
            "SET",
            "RETURNING",
            "RIGHT",
            "FULL",
            "INDEXED",
            "DO",
        ):
            return self.ident()
        return None

    def name_list(self) -> list[str]:
        names = [self.ident()]
        while self.accept(","):
            names.append(self.ident())
        return names

    def expr_list(self) -> list[A.Expr]:
        items = [self.expr()]
        while self.accept(","):
            items.append(self.expr())
        return items

    # ------------------------------------------------------------ INSERT / UPDATE / DELETE

    def conflict_clause(self) -> str:
        if self.accept("OR"):
            t = self.advance()
            if t.upper not in ("ABORT", "REPLACE", "IGNORE", "FAIL", "ROLLBACK"):
                raise self.error("expected ABORT, REPLACE, IGNORE, FAIL or ROLLBACK after OR", t)
            return t.upper
        return "ABORT"

    def returning(self) -> list[A.ResultColumn] | None:
        if not self.accept("RETURNING"):
            return None
        cols = [self.result_column()]
        while self.accept(","):
            cols.append(self.result_column())
        return cols

    def insert(self) -> A.Insert:
        start = self.tok.pos
        if self.accept("REPLACE"):
            action = "REPLACE"
        else:
            self.expect("INSERT")
            action = self.conflict_clause()
        self.expect("INTO", "after INSERT")
        table = self.ident("a table name")
        alias = self.ident() if self.accept("AS") else None
        columns = None
        if self.accept("("):
            columns = self.name_list()
            self.expect(")", "after the column list")
        source: A.Select | None
        if self.accept("DEFAULT"):
            self.expect("VALUES")
            source = None
        elif self.at("SELECT", "VALUES", "WITH"):
            source = self.select()
        else:
            raise self.error(f"expected VALUES or SELECT, found {self.describe(self.tok)}")
        ins = A.Insert(table, columns, source, action, alias=alias)
        if self.at("ON") and self.peek().upper == "CONFLICT":
            ustart = self.tok.pos
            self.advance()
            self.advance()
            target = None
            if self.accept("("):
                target = self.name_list()
                self.expect(")")
            self.expect("DO", "after ON CONFLICT")
            if self.accept("NOTHING"):
                ins.upsert = self.mark(A.Upsert(target, "NOTHING"), ustart)
            else:
                self.expect("UPDATE", "after DO")
                self.expect("SET")
                assigns = self.assignments()
                where = self.expr() if self.accept("WHERE") else None
                ins.upsert = self.mark(A.Upsert(target, "UPDATE", assigns, where), ustart)
        ins.returning = self.returning()
        return self.mark(ins, start)

    def assignments(self) -> list[tuple[list[str], A.Expr]]:
        out = []
        while True:
            if self.accept("("):
                names = self.name_list()
                self.expect(")")
            else:
                names = [self.ident("a column name")]
            self.expect("=", "in SET")
            out.append((names, self.expr()))
            if not self.accept(","):
                return out

    def update(self) -> A.Update:
        start = self.tok.pos
        self.expect("UPDATE")
        action = self.conflict_clause()
        table = self.ident("a table name")
        alias = None
        if self.accept("AS"):
            alias = self.ident()
        elif self.is_ident() and not self.at("SET"):
            alias = self.ident()
        self.expect("SET", "after the table name")
        upd = A.Update(table, self.assignments(), on_conflict=action, alias=alias)
        if self.accept("FROM"):
            upd.from_ = self.from_clause()
        if self.accept("WHERE"):
            upd.where = self.expr()
        upd.returning = self.returning()
        return self.mark(upd, start)

    def delete(self) -> A.Delete:
        start = self.tok.pos
        self.expect("DELETE")
        self.expect("FROM", "after DELETE")
        table = self.ident("a table name")
        alias = self.ident() if self.accept("AS") else None
        d = A.Delete(table, alias=alias)
        if self.accept("WHERE"):
            d.where = self.expr()
        d.returning = self.returning()
        return self.mark(d, start)

    # ------------------------------------------------------------ DDL

    def if_not_exists(self) -> bool:
        if self.accept("IF"):
            self.expect("NOT")
            self.expect("EXISTS")
            return True
        return False

    def create(self) -> A.Node:
        start = self.tok.pos
        self.expect("CREATE")
        self.accept("TEMP", "TEMPORARY")
        unique = bool(self.accept("UNIQUE"))
        if self.accept("TABLE"):
            return self.create_table(start)
        if self.accept("INDEX"):
            ine = self.if_not_exists()
            name = self.ident("an index name")
            self.expect("ON")
            table = self.ident("a table name")
            self.expect("(")
            cols = []
            while True:
                if not self.is_ident() or self.peek().value in ("(", "+", "-", "*", "/", "||"):
                    raise NotSupported("expression indexes are not supported yet", self.sql, self.tok.pos, "index plain columns")
                cols.append(self.ident("a column name"))
                if self.accept("COLLATE"):
                    coll = self.ident()
                    if coll.upper() != "BINARY":
                        raise NotSupported("indexes support only the BINARY collation", self.sql, self.tok.pos)
                self.accept("ASC", "DESC")
                if not self.accept(","):
                    break
            self.expect(")")
            where = self.expr() if self.accept("WHERE") else None
            return self.mark(A.CreateIndex(name, table, cols, unique, ine, where), start)
        if unique:
            raise self.error("expected INDEX after CREATE UNIQUE")
        if self.accept("VIEW"):
            ine = self.if_not_exists()
            name = self.ident("a view name")
            cols = None
            if self.accept("("):
                cols = self.name_list()
                self.expect(")")
            self.expect("AS")
            return self.mark(A.CreateView(name, cols, self.select(), ine), start)
        if self.at("TRIGGER", "VIRTUAL"):
            raise NotSupported(f"CREATE {self.tok.upper} is not supported yet", self.sql, self.tok.pos)
        raise self.error(f"expected TABLE, INDEX or VIEW after CREATE, found {self.describe(self.tok)}")

    def create_table(self, start: int) -> A.CreateTable:
        ine = self.if_not_exists()
        name = self.ident("a table name")
        if self.accept("."):
            name = self.ident("a table name")
        if self.accept("AS"):
            return self.mark(A.CreateTable(name, [], if_not_exists=ine, as_select=self.select()), start)
        self.expect("(", "before the column definitions")
        columns: list[A.ColumnDef] = []
        constraints: list[A.TableConstraint] = []
        while True:
            if self.at("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
                constraints.append(self.table_constraint())
            else:
                columns.append(self.column_def())
            if not self.accept(","):
                break
        self.expect(")", "after the column definitions")
        without_rowid = False
        if self.at("STRICT") or (self.at(",") and self.peek().upper == "STRICT"):
            raise NotSupported(
                "STRICT tables are not supported yet", self.sql, self.tok.pos, "remove STRICT: columns then use type affinity"
            )
        if self.accept("WITHOUT"):
            t = self.advance()
            if t.upper != "ROWID":
                raise self.error("expected ROWID after WITHOUT", t)
            without_rowid = True
        if not columns:
            raise SQLError("a table needs at least one column", self.sql, start)
        return self.mark(A.CreateTable(name, columns, constraints, ine, without_rowid=without_rowid), start)

    def type_name(self) -> str:
        parts = []
        while self.tok.kind == "word" and self.tok.upper not in RESERVED and self.tok.upper not in ("AUTOINCREMENT", "ASC", "DESC"):
            parts.append(str(self.advance().value))
        if parts and self.accept("("):
            args = [str(self.advance().value)]
            if self.accept(","):
                args.append(str(self.advance().value))
            self.expect(")")
            return " ".join(parts) + "(" + ",".join(args) + ")"
        return " ".join(parts)

    def column_def(self) -> A.ColumnDef:
        start = self.tok.pos
        col = A.ColumnDef(self.ident("a column name"))
        col.type_name = self.type_name()
        while True:
            if self.accept("CONSTRAINT"):
                self.ident()
            if self.accept("PRIMARY"):
                self.expect("KEY")
                col.primary_key = True
                if self.accept("DESC"):
                    col.pk_desc = True
                else:
                    self.accept("ASC")
                self.skip_conflict()
                if self.accept("AUTOINCREMENT"):
                    col.autoincrement = True
            elif self.accept("NOT"):
                self.expect("NULL")
                col.not_null = True
                self.skip_conflict()
            elif self.accept("NULL"):
                pass
            elif self.accept("UNIQUE"):
                col.unique = True
                self.skip_conflict()
            elif self.accept("DEFAULT"):
                if self.accept("("):
                    col.default = self.expr()
                    self.expect(")")
                elif self.at("-", "+"):
                    s = self.tok.pos
                    op = self.advance().value
                    col.default = self.mark(A.Unary(str(op), self.primary()), s)
                else:
                    col.default = self.primary()
            elif self.accept("CHECK"):
                self.expect("(")
                col.check.append(self.expr())
                self.expect(")")
            elif self.accept("COLLATE"):
                col.collation = self.ident("a collation name").upper()
            elif self.accept("REFERENCES"):
                col.references = self.references()
            elif self.at("GENERATED", "AS"):
                raise NotSupported("generated columns are not supported yet", self.sql, self.tok.pos)
            else:
                break
        col.pos = start
        return col

    def references(self) -> tuple[str, list[str]]:
        table = self.ident("a table name")
        cols = []
        if self.accept("("):
            cols = self.name_list()
            self.expect(")")
        while self.at("ON", "MATCH", "DEFERRABLE", "NOT"):  # accepted and ignored, like SQLite with foreign_keys off
            if self.accept("ON"):
                self.advance()
                if self.accept("SET"):
                    self.advance()
                elif self.accept("NO"):
                    self.advance()
                else:
                    self.advance()
            elif self.accept("MATCH"):
                self.advance()
            else:
                self.accept("NOT")
                self.advance()
                if self.accept("INITIALLY"):
                    self.advance()
        return table, cols

    def skip_conflict(self) -> None:
        if self.at("ON") and self.peek().upper == "CONFLICT":
            self.advance()
            self.advance()
            self.advance()

    def table_constraint(self) -> A.TableConstraint:
        start = self.tok.pos
        name = None
        if self.accept("CONSTRAINT"):
            name = self.ident()
        if self.accept("PRIMARY"):
            self.expect("KEY")
            self.expect("(")
            cols = self.indexed_names()
            self.expect(")")
            self.skip_conflict()
            return self.mark(A.TableConstraint("PRIMARY KEY", cols, name=name), start)
        if self.accept("UNIQUE"):
            self.expect("(")
            cols = self.indexed_names()
            self.expect(")")
            self.skip_conflict()
            return self.mark(A.TableConstraint("UNIQUE", cols, name=name), start)
        if self.accept("CHECK"):
            self.expect("(")
            e = self.expr()
            self.expect(")")
            return self.mark(A.TableConstraint("CHECK", check=e, name=name), start)
        self.expect("FOREIGN")
        self.expect("KEY")
        self.expect("(")
        cols = self.name_list()
        self.expect(")")
        self.expect("REFERENCES")
        return self.mark(A.TableConstraint("FOREIGN KEY", cols, name=name, references=self.references()), start)

    def indexed_names(self) -> list[str]:
        names = []
        while True:
            names.append(self.ident("a column name"))
            if self.accept("COLLATE"):
                self.ident()
            self.accept("ASC", "DESC")
            if not self.accept(","):
                return names

    def drop(self) -> A.Drop:
        start = self.tok.pos
        self.expect("DROP")
        t = self.advance()
        if t.upper not in ("TABLE", "INDEX", "VIEW"):
            raise self.error("expected TABLE, INDEX or VIEW after DROP", t)
        if_exists = False
        if self.accept("IF"):
            self.expect("EXISTS")
            if_exists = True
        name = self.ident("a name")
        if self.accept("."):
            name = self.ident("a name")
        return self.mark(A.Drop(t.upper, name, if_exists), start)

    def alter(self) -> A.AlterTable:
        start = self.tok.pos
        self.expect("ALTER")
        self.expect("TABLE")
        table = self.ident("a table name")
        if self.accept("RENAME"):
            if self.accept("TO"):
                return self.mark(A.AlterTable(table, "RENAME", new_name=self.ident("a new table name")), start)
            self.accept("COLUMN")
            old = self.ident("a column name")
            self.expect("TO")
            return self.mark(A.AlterTable(table, "RENAME COLUMN", new_name=self.ident("a new column name"), old_column=old), start)
        if self.accept("ADD"):
            self.accept("COLUMN")
            return self.mark(A.AlterTable(table, "ADD COLUMN", column=self.column_def()), start)
        if self.accept("DROP"):
            self.accept("COLUMN")
            return self.mark(A.AlterTable(table, "DROP COLUMN", old_column=self.ident("a column name")), start)
        raise self.error("expected RENAME, ADD or DROP after ALTER TABLE")

    def transaction(self) -> A.Transaction:
        start = self.tok.pos
        t = self.advance()
        kw = t.upper
        if kw == "BEGIN":
            mode = None
            if self.at("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
                mode = self.advance().upper
            self.accept("TRANSACTION")
            return self.mark(A.Transaction("BEGIN", mode=mode), start)
        if kw in ("COMMIT", "END"):
            self.accept("TRANSACTION")
            return self.mark(A.Transaction("COMMIT"), start)
        if kw == "ROLLBACK":
            self.accept("TRANSACTION")
            if self.accept("TO"):
                self.accept("SAVEPOINT")
                return self.mark(A.Transaction("ROLLBACK", savepoint=self.ident("a savepoint name")), start)
            return self.mark(A.Transaction("ROLLBACK"), start)
        if kw == "SAVEPOINT":
            return self.mark(A.Transaction("SAVEPOINT", savepoint=self.ident("a savepoint name")), start)
        self.accept("SAVEPOINT")
        return self.mark(A.Transaction("RELEASE", savepoint=self.ident("a savepoint name")), start)

    def pragma(self) -> A.Pragma:
        start = self.tok.pos
        self.expect("PRAGMA")
        name = self.ident("a pragma name").lower()
        if self.accept("."):
            name = self.ident("a pragma name").lower()
        p = A.Pragma(name)
        if self.accept("="):
            p.value = self.pragma_value()
        elif self.accept("("):
            p.argument = str(self.pragma_value())
            self.expect(")")
        return self.mark(p, start)

    def pragma_value(self) -> object:
        neg = bool(self.accept("-"))
        t = self.advance()
        if t.kind == "number":
            return -t.value if neg else t.value  # type: ignore[operator]
        if t.kind in ("string", "ident", "word"):
            return t.value
        raise self.error("expected a pragma value", t)

    # ------------------------------------------------------------ expressions

    def expr(self) -> A.Expr:
        return self.or_expr()

    def or_expr(self) -> A.Expr:
        start = self.tok.pos
        left = self.and_expr()
        while self.accept("OR"):
            left = self.mark(A.Binary("OR", left, self.and_expr()), start)
        return left

    def and_expr(self) -> A.Expr:
        start = self.tok.pos
        left = self.not_expr()
        while self.accept("AND"):
            left = self.mark(A.Binary("AND", left, self.not_expr()), start)
        return left

    def not_expr(self) -> A.Expr:
        start = self.tok.pos
        if self.accept("NOT"):
            return self.mark(A.Unary("NOT", self.not_expr()), start)
        return self.equality()

    def equality(self) -> A.Expr:
        start = self.tok.pos
        left = self.comparison()
        while True:
            if self.at("=", "==", "!=", "<>"):
                op = str(self.advance().value)
                op = {"==": "=", "<>": "!="}.get(op, op)
                left = self.mark(A.Binary(op, left, self.comparison()), start)
            elif self.at("IS"):
                self.advance()
                neg = bool(self.accept("NOT"))
                if self.accept("DISTINCT"):
                    self.expect("FROM")
                    neg = not neg
                right = self.comparison()
                left = self.mark(A.Binary("IS NOT" if neg else "IS", left, right), start)
            elif self.at("ISNULL"):
                self.advance()
                left = self.mark(A.IsNull(False, left), start)
            elif self.at("NOTNULL"):
                self.advance()
                left = self.mark(A.IsNull(True, left), start)
            elif self.at_seq("NOT", "NULL"):
                self.advance()
                self.advance()
                left = self.mark(A.IsNull(True, left), start)
            elif self.at("IN", "LIKE", "GLOB", "BETWEEN", "REGEXP", "MATCH") or (
                self.at("NOT") and self.peek().upper in ("IN", "LIKE", "GLOB", "BETWEEN", "REGEXP", "MATCH")
            ):
                neg = bool(self.accept("NOT"))
                kw = self.advance().upper
                if kw in ("REGEXP", "MATCH"):
                    raise NotSupported(f"{kw} is not supported", self.sql, start)
                if kw == "IN":
                    left = self.mark(self.in_rest(neg, left), start)
                elif kw == "BETWEEN":
                    low = self.comparison()
                    self.expect("AND", "in BETWEEN")
                    high = self.comparison()
                    left = self.mark(A.Between(neg, left, low, high), start)
                else:
                    pattern = self.comparison()
                    esc = self.comparison() if self.accept("ESCAPE") else None
                    left = self.mark(A.Like(kw, neg, left, pattern, esc), start)
            else:
                return left

    def in_rest(self, neg: bool, left: A.Expr) -> A.Expr:
        if not self.at("("):
            name = self.ident("a list in parentheses or a table name")  # x IN table
            sel = A.Select([A.SelectCore([A.ResultColumn(None, star=True)], A.TableRef(name))])
            return A.InSelect(neg, left, sel)
        self.advance()
        if self.at("SELECT", "VALUES", "WITH"):
            sel = self.select()
            self.expect(")", "after the subquery")
            return A.InSelect(neg, left, sel)
        if self.accept(")"):
            return A.InList(neg, left, [])
        items = self.expr_list()
        self.expect(")", "after the IN list")
        return A.InList(neg, left, items)

    def comparison(self) -> A.Expr:
        start = self.tok.pos
        left = self.bitwise()
        while self.at("<", "<=", ">", ">="):
            op = str(self.advance().value)
            left = self.mark(A.Binary(op, left, self.bitwise()), start)
        return left

    def bitwise(self) -> A.Expr:
        start = self.tok.pos
        left = self.additive()
        while self.at("&", "|", "<<", ">>"):
            op = str(self.advance().value)
            left = self.mark(A.Binary(op, left, self.additive()), start)
        return left

    def additive(self) -> A.Expr:
        start = self.tok.pos
        left = self.multiplicative()
        while self.at("+", "-"):
            op = str(self.advance().value)
            left = self.mark(A.Binary(op, left, self.multiplicative()), start)
        return left

    def multiplicative(self) -> A.Expr:
        start = self.tok.pos
        left = self.concat()
        while self.at("*", "/", "%"):
            op = str(self.advance().value)
            left = self.mark(A.Binary(op, left, self.concat()), start)
        return left

    def concat(self) -> A.Expr:
        start = self.tok.pos
        left = self.unary()
        while self.at("||", "->", "->>"):
            op = str(self.advance().value)
            if op != "||":
                raise NotSupported("JSON operators are not supported yet", self.sql, start)
            left = self.mark(A.Binary("||", left, self.unary()), start)
        return left

    def unary(self) -> A.Expr:
        start = self.tok.pos
        if self.at("-", "+", "~"):
            op = str(self.advance().value)
            operand = self.unary()
            if (
                op == "-"
                and isinstance(operand, A.Literal)
                and isinstance(operand.value, (int, float))
                and not isinstance(operand.value, bool)
            ):
                v = operand.value
                if isinstance(v, float) and v == 2**63 and self.sql[operand.pos : operand.pos + 19].isdigit():
                    return self.mark(A.Literal(-(2**63)), start)
                return self.mark(A.Literal(-v), start)
            return self.mark(A.Unary(op, operand), start)
        e = self.primary()
        while self.at("COLLATE"):
            self.advance()
            e = self.mark(A.Collate(e, self.ident("a collation name").upper()), start)
        return e

    def primary(self) -> A.Expr:
        t = self.tok
        start = t.pos
        if t.kind == "number":
            self.advance()
            return self.mark(A.Literal(t.value), start)
        if t.kind == "string":
            self.advance()
            return self.mark(A.Literal(t.value), start)
        if t.kind == "blob":
            self.advance()
            return self.mark(A.Literal(t.value), start)
        if t.kind == "param":
            self.advance()
            return self.mark(self.param(str(t.value), t), start)
        if t.kind == "op" and t.value == "(":
            self.advance()
            if self.at("SELECT", "VALUES", "WITH"):
                sel = self.select()
                self.expect(")", "after the subquery")
                return self.mark(A.Subquery(sel), start)
            e = self.expr()
            if self.at(","):
                raise NotSupported("row values like (a, b) are not supported yet", self.sql, self.tok.pos)
            self.expect(")", "to close the parenthesis")
            e.pos = start
            e.text = self.span(start)
            return e
        if t.kind == "word":
            kw = t.upper
            if kw == "NULL":
                self.advance()
                return self.mark(A.Literal(None), start)
            if kw in ("TRUE", "FALSE") and self.peek().value != "(":
                self.advance()
                return self.mark(A.Literal(1 if kw == "TRUE" else 0), start)
            if kw in ("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"):
                self.advance()
                fn = {"CURRENT_DATE": "date", "CURRENT_TIME": "time", "CURRENT_TIMESTAMP": "datetime"}[kw]
                return self.mark(A.Func(fn, [A.Literal("now")]), start)
            if kw == "CASE":
                return self.case()
            if kw == "CAST":
                self.advance()
                self.expect("(")
                e = self.expr()
                self.expect("AS", "in CAST")
                type_name = self.type_name()
                self.expect(")")
                return self.mark(A.Cast(e, type_name), start)
            if kw == "EXISTS" or (kw == "NOT" and self.peek().upper == "EXISTS"):
                neg = bool(self.accept("NOT"))
                self.advance()
                self.expect("(")
                sel = self.select()
                self.expect(")")
                e = self.mark(A.Exists(sel), start)
                return self.mark(A.Unary("NOT", e), start) if neg else e
            if kw == "SELECT":
                raise self.error("a subquery must be in parentheses", hint="write (SELECT ...)")
        if self.is_ident(t):
            dquoted = t.kind == "ident" and self.sql[t.pos] == '"'
            name = self.ident()
            if self.at("("):
                return self.function(name, start)
            if self.accept("."):
                col = self.ident("a column name")
                if self.accept("."):  # schema.table.column
                    name, col = col, self.ident("a column name")
                return self.mark(A.Column(name, col), start)
            return self.mark(A.Column(None, name, quoted=dquoted), start)
        hint = ""
        if t.kind == "word" and t.upper in RESERVED:
            hint = f'{t.value} is a reserved word: write "{t.value}" in double quotes to use it as a name'
        raise self.error(f"expected an expression, found {self.describe(t)}", hint=hint)

    def param(self, text: str, t: Token) -> A.Param:
        if text == "?":
            self.param_count += 1
            return A.Param(self.param_count)
        if text.startswith("?"):
            n = int(text[1:])
            if n < 1:
                raise self.error("parameter numbers start at 1", t)
            self.param_count = max(self.param_count, n)
            return A.Param(n)
        name = text[1:]
        if name not in self.named:
            self.param_count += 1
            self.named[name] = self.param_count
        return A.Param(self.named[name], name)

    def case(self) -> A.Case:
        start = self.tok.pos
        self.expect("CASE")
        operand = None if self.at("WHEN") else self.expr()
        whens = []
        while self.accept("WHEN"):
            cond = self.expr()
            self.expect("THEN", "after WHEN")
            whens.append((cond, self.expr()))
        if not whens:
            raise self.error("CASE needs at least one WHEN")
        otherwise = self.expr() if self.accept("ELSE") else None
        self.expect("END", "to close CASE")
        return self.mark(A.Case(operand, whens, otherwise), start)

    def function(self, name: str, start: int) -> A.Func:
        self.expect("(")
        fn = A.Func(name.lower(), [])
        if self.accept("*"):
            fn.star = True
        elif not self.at(")"):
            if self.accept("DISTINCT"):
                fn.distinct = True
            fn.args = self.expr_list()
            if self.at("ORDER"):
                raise NotSupported("ORDER BY inside an aggregate is not supported yet", self.sql, self.tok.pos)
        self.expect(")", f"to close {name}(")
        if self.accept("FILTER"):
            self.expect("(")
            self.expect("WHERE")
            fn.filter = self.expr()
            self.expect(")")
        if self.accept("OVER"):
            if self.at("("):
                fn.over = self.window_spec()
            else:
                fn.over = self.ident("a window name").lower()
        return self.mark(fn, start)

    def window_spec(self) -> A.WindowSpec:
        start = self.tok.pos
        self.expect("(")
        base = None
        if self.is_ident() and not self.at("PARTITION", "ORDER", "ROWS", "RANGE", "GROUPS"):
            base = self.ident().lower()
        partition: list[A.Expr] = []
        order: list[A.OrderItem] = []
        if self.accept("PARTITION"):
            self.expect("BY")
            partition = self.expr_list()
        if self.accept("ORDER"):
            self.expect("BY")
            order = self.order_list()
        frame = None
        if self.at("ROWS", "RANGE", "GROUPS"):
            unit = self.advance().upper
            if unit == "GROUPS":
                raise NotSupported("GROUPS frames are not supported yet", self.sql, start)
            if self.accept("BETWEEN"):
                lo = self.frame_bound()
                self.expect("AND")
                hi = self.frame_bound()
            else:
                lo = self.frame_bound()
                hi = ("CURRENT", 0)
            if self.accept("EXCLUDE"):
                raise NotSupported("EXCLUDE in window frames is not supported yet", self.sql, start)
            frame = (unit, lo, hi)
        self.expect(")", "to close the window definition")
        return self.mark(A.WindowSpec(partition, order, frame, base), start)

    def frame_bound(self) -> tuple[str, object]:
        if self.accept("UNBOUNDED"):
            t = self.advance()
            return ("UNBOUNDED_PRECEDING" if t.upper == "PRECEDING" else "UNBOUNDED_FOLLOWING", 0)
        if self.accept("CURRENT"):
            self.expect("ROW")
            return ("CURRENT", 0)
        e = self.expr()
        t = self.advance()
        if t.upper not in ("PRECEDING", "FOLLOWING"):
            raise self.error("expected PRECEDING or FOLLOWING", t)
        return (t.upper, e)


def parse(sql: str) -> list[A.Node]:
    """Parse a script of one or more statements."""
    return Parser(sql).parse_script()


def parse_one(sql: str) -> tuple[A.Node, int]:
    """Parse exactly one statement. Returns it and the number of parameters it uses."""
    p = Parser(sql)
    statements = p.parse_script()
    if not statements:
        raise SQLError("the SQL text is empty")
    if len(statements) > 1:
        raise SQLError("you can only execute one statement at a time", sql, statements[1].pos, "use executescript() for several statements")
    return statements[0], p.param_count


def parse_expr(sql: str) -> A.Expr:
    p = Parser(sql)
    e = p.expr()
    if p.tok.kind != "eof":
        raise p.error(f"unexpected {p.describe(p.tok)} after the expression")
    return e


def split_statements(sql: str) -> list[str]:
    """Split a script into statement texts, respecting quotes, comments and BEGIN...END blocks."""
    tokens = tokenize(sql)
    out = []
    start = 0
    for t in tokens:
        if (t.kind == "op" and t.value == ";") or t.kind == "eof":
            text = sql[start : t.pos].strip()
            if text and any(tok.kind != "eof" for tok in tokenize(text)):
                out.append(text)
            start = t.end
    return out
