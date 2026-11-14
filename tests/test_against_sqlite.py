"""Every query here runs on both Quarry and SQLite, and the answers must match exactly.

Queries marked ``-- ordered`` compare row order too; the rest compare as multisets.
"""

import pytest

QUERIES = """
SELECT * FROM dept
SELECT name, budget FROM dept WHERE budget > 50000
SELECT id, name FROM emp WHERE dept_id = 3 ORDER BY id -- ordered
SELECT id FROM emp WHERE dept_id IS NULL
SELECT id FROM emp WHERE salary BETWEEN 50000 AND 80000
SELECT id FROM emp WHERE salary NOT BETWEEN 50000 AND 150000
SELECT id FROM emp WHERE dept_id IN (1, 3, 5) AND salary > 90000
SELECT id FROM emp WHERE dept_id NOT IN (1, 2, NULL)
SELECT id FROM emp WHERE dept_id NOT IN (1, 2)
SELECT id, name FROM emp WHERE name LIKE 'a%'
SELECT id FROM emp WHERE name LIKE '_e%' ESCAPE '\\'
SELECT id FROM emp WHERE name GLOB '*a[0-9]'
SELECT id FROM emp WHERE title = 'engineer'
SELECT id FROM emp WHERE title = 'ENGINEER' COLLATE BINARY
SELECT DISTINCT title FROM emp
SELECT DISTINCT dept_id, lower(title) FROM emp
SELECT count(DISTINCT title), count(DISTINCT title COLLATE BINARY) FROM emp
SELECT count(*), count(dept_id), count(DISTINCT dept_id), sum(salary), avg(salary), min(salary), max(salary), total(bonus) FROM emp
SELECT dept_id, count(*), sum(salary), round(avg(bonus), 2) FROM emp GROUP BY dept_id
SELECT dept_id, count(*) AS n FROM emp GROUP BY dept_id HAVING n > 12
SELECT dept_id, max(salary) FROM emp GROUP BY 1 HAVING max(salary) > 150000
SELECT lower(title), group_concat(id, '|') FROM (SELECT * FROM emp ORDER BY id) GROUP BY title
SELECT count(*) FROM emp WHERE 0
SELECT sum(salary), avg(salary), max(name) FROM emp WHERE 0
SELECT dept_id, count(*) FROM emp WHERE salary > 1000000 GROUP BY dept_id
SELECT e.name, d.name FROM emp e JOIN dept d ON d.id = e.dept_id
SELECT e.name, d.name FROM emp e LEFT JOIN dept d ON d.id = e.dept_id
SELECT d.name, count(e.id) FROM dept d LEFT JOIN emp e ON e.dept_id = d.id GROUP BY d.id
SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept_id = d.id AND e.salary > 140000
SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept_id = d.id WHERE e.id IS NULL
SELECT e.name, m.name FROM emp e JOIN emp m ON m.id = e.manager_id WHERE e.salary > m.salary
SELECT e.name, p.code FROM emp e, project p WHERE p.lead_id = e.id
SELECT e.id, a.code, a.hours FROM emp e JOIN assign a ON a.emp_id = e.id WHERE a.hours > 30
SELECT p.code, e.name, d.name FROM project p JOIN emp e ON e.id = p.lead_id JOIN dept d ON d.id = e.dept_id
SELECT * FROM assign JOIN project USING (code) WHERE hours < 5
SELECT * FROM dept NATURAL JOIN (SELECT id, name AS dname FROM dept)
SELECT e.name FROM emp e JOIN dept d ON d.region = e.title
SELECT e.name, d.budget FROM emp e CROSS JOIN dept d WHERE d.id = 1 AND e.id < 5
SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE region = 'north')
SELECT name FROM emp WHERE dept_id NOT IN (SELECT id FROM dept WHERE budget > 40000)
SELECT name FROM emp WHERE dept_id NOT IN (SELECT id FROM dept WHERE budget IS NULL OR budget > 40000)
SELECT name FROM emp e WHERE EXISTS (SELECT 1 FROM assign a WHERE a.emp_id = e.id AND a.hours > 35)
SELECT name FROM emp e WHERE NOT EXISTS (SELECT 1 FROM assign a WHERE a.emp_id = e.id)
SELECT name, (SELECT name FROM dept WHERE id = emp.dept_id) FROM emp
SELECT name, (SELECT count(*) FROM assign WHERE emp_id = emp.id) AS n FROM emp WHERE n > 2
SELECT d.name, (SELECT max(salary) FROM emp WHERE dept_id = d.id) FROM dept d
SELECT * FROM (SELECT dept_id, avg(salary) AS a FROM emp GROUP BY dept_id) WHERE a > 90000
SELECT name FROM emp WHERE salary > (SELECT avg(salary) FROM emp)
SELECT name FROM emp e WHERE salary = (SELECT max(salary) FROM emp WHERE dept_id = e.dept_id)
SELECT id FROM emp ORDER BY salary DESC, id LIMIT 10 -- ordered
SELECT id FROM emp ORDER BY salary, id LIMIT 5 OFFSET 3 -- ordered
SELECT id FROM emp ORDER BY salary DESC NULLS LAST, id LIMIT 15 -- ordered
SELECT id FROM emp ORDER BY salary NULLS FIRST, id LIMIT 15 -- ordered
SELECT id, title FROM emp ORDER BY title, id -- ordered
SELECT id, title FROM emp ORDER BY title COLLATE BINARY DESC, id -- ordered
SELECT name AS n FROM emp ORDER BY n -- ordered
SELECT id, salary * 2 AS s FROM emp ORDER BY 2 DESC, 1 LIMIT 7 -- ordered
SELECT dept_id, count(*) FROM emp GROUP BY dept_id ORDER BY count(*) DESC, dept_id -- ordered
SELECT n, r, t, b, x FROM nums ORDER BY n, r, t, b, x -- ordered
SELECT x FROM nums ORDER BY x DESC -- ordered
SELECT DISTINCT x FROM nums
SELECT n FROM nums WHERE n > 1
SELECT n FROM nums WHERE n = '10'
SELECT t FROM nums WHERE t = 10
SELECT t FROM nums WHERE t > 2
SELECT x FROM nums WHERE x = 10
SELECT x FROM nums WHERE x < 'a'
SELECT r FROM nums WHERE r >= 2.5
SELECT b FROM nums WHERE b IN (1, 2, '10')
SELECT n + 1, n * 2, n - r, n / 2, n % 3, -n FROM nums
SELECT t || '!', n || r, typeof(n), typeof(r), typeof(t), typeof(b), typeof(x) FROM nums
SELECT n & 6, n | 1, n << 2, n >> 1, ~n FROM nums WHERE typeof(n) = 'integer'
SELECT abs(n), length(t), lower(t), upper(t), hex(b), quote(x) FROM nums
SELECT coalesce(n, r, t, 'none'), ifnull(x, 'dflt'), nullif(n, 1), iif(n > 1, 'big', 'small') FROM nums
SELECT CAST(t AS INTEGER), CAST(r AS TEXT), CAST(n AS REAL), CAST(x AS NUMERIC), CAST(t AS BLOB) FROM nums
SELECT max(n), min(n), max(t), min(t), max(b), min(x) FROM nums
SELECT sum(n), total(n), avg(r), count(x) FROM nums WHERE typeof(n) <> 'text'
SELECT n, count(*) FROM nums GROUP BY n
SELECT t, count(*) FROM nums GROUP BY t
SELECT CASE WHEN salary > 100000 THEN 'high' WHEN salary > 60000 THEN 'mid' ELSE 'low' END AS band, count(*) FROM emp GROUP BY band
SELECT CASE dept_id WHEN 1 THEN 'one' WHEN 2 THEN 'two' END FROM emp
SELECT substr(name, 2, 3), substr(name, -2), instr(name, 'a'), replace(name, 'a', 'A'), trim('  x  '), ltrim(name, 'ab') FROM emp
SELECT round(bonus), round(bonus, 1), abs(-salary), sign(salary - 90000) FROM emp
SELECT printf('%s earns %d', name, salary), printf('%.2f', bonus) FROM emp WHERE id < 20
SELECT date(hired), date(hired, '+1 month'), strftime('%Y', hired), julianday(hired) - julianday('2010-01-01') FROM emp
SELECT strftime('%m', hired) AS m, count(*) FROM emp GROUP BY m
SELECT date(hired, 'start of year', '+6 months', '-1 day') FROM emp
SELECT 1, 'two', 3.5, NULL, X'ff', -7, 1e3, 0x1F
SELECT 7 / 2, -7 / 2, 7 % -3, 7.0 / 2, 1 / 0, 5 % 0, 2.5 % 2
SELECT 1 = 1.0, '1' = 1, 'a' < 1, NULL = NULL, NULL IS NULL, 1 IS NOT 2, 3 IN (1, NULL), 3 NOT IN (1, NULL)
SELECT 'abc' LIKE 'ABC', 'abc' GLOB 'ABC', 'a%c' LIKE 'a\\%c' ESCAPE '\\'
SELECT 9223372036854775807 + 1, -9223372036854775808 - 1, 4611686018427387904 * 2
SELECT max(1, 'a', 2.5), min(3, 1, 2), max(1, NULL)
SELECT typeof(1 + 1.0), typeof('1' + 1), typeof(1 || 1), typeof(NULL)
SELECT name FROM emp WHERE id = 7 UNION SELECT name FROM dept WHERE id < 3
SELECT dept_id FROM emp UNION ALL SELECT id FROM dept
SELECT dept_id FROM emp INTERSECT SELECT id FROM dept
SELECT id FROM dept EXCEPT SELECT dept_id FROM emp WHERE salary > 100000
SELECT id, name FROM dept UNION SELECT id, name FROM emp WHERE id < 4 ORDER BY 1, 2 -- ordered
SELECT id FROM emp WHERE id < 10 UNION SELECT id FROM emp WHERE id > 115 ORDER BY id DESC LIMIT 4 -- ordered
VALUES (1, 'a'), (2, 'b'), (3, NULL)
SELECT * FROM (VALUES (1, 2), (3, 4)) AS v
WITH big AS (SELECT * FROM emp WHERE salary > 120000) SELECT dept_id, count(*) FROM big GROUP BY dept_id
WITH a AS (SELECT id FROM dept), b AS (SELECT id * 10 AS id FROM a) SELECT * FROM a, b WHERE b.id < 40
WITH RECURSIVE cnt(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM cnt WHERE n < 100) SELECT sum(n), count(*) FROM cnt
WITH RECURSIVE chain(id, depth) AS (SELECT id, 0 FROM emp WHERE manager_id IS NULL UNION ALL SELECT e.id, c.depth + 1 FROM emp e JOIN chain c ON e.manager_id = c.id) SELECT depth, count(*) FROM chain GROUP BY depth
WITH RECURSIVE fib(a, b) AS (SELECT 0, 1 UNION ALL SELECT b, a + b FROM fib WHERE b < 1000) SELECT a FROM fib
WITH RECURSIVE dup(x) AS (SELECT 1 UNION SELECT x % 5 + 1 FROM dup) SELECT x FROM dup
SELECT id, salary, row_number() OVER (ORDER BY salary DESC, id) FROM emp
SELECT id, dept_id, rank() OVER (PARTITION BY dept_id ORDER BY salary DESC) FROM emp
SELECT id, dense_rank() OVER (ORDER BY dept_id), percent_rank() OVER (ORDER BY dept_id), cume_dist() OVER (ORDER BY dept_id) FROM emp
SELECT id, ntile(4) OVER (ORDER BY id) FROM emp
SELECT id, lag(salary) OVER (ORDER BY id), lead(salary, 2, -1) OVER (ORDER BY id) FROM emp
SELECT id, sum(salary) OVER (ORDER BY id) FROM emp
SELECT id, sum(salary) OVER (PARTITION BY dept_id) FROM emp
SELECT id, avg(salary) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING) FROM emp
SELECT id, count(*) OVER (ORDER BY dept_id) FROM emp
SELECT id, first_value(name) OVER w, last_value(name) OVER w, nth_value(name, 2) OVER w FROM emp WINDOW w AS (PARTITION BY dept_id ORDER BY id)
SELECT id, max(salary) OVER (ORDER BY id ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) FROM emp
SELECT dept_id, sum(salary), rank() OVER (ORDER BY sum(salary) DESC) FROM emp GROUP BY dept_id
SELECT count(*) FILTER (WHERE salary > 100000), sum(salary) FILTER (WHERE dept_id = 1) FROM emp
SELECT lower(title), count(*) FROM emp GROUP BY title
SELECT upper(title), count(*) FROM emp GROUP BY upper(title)
SELECT code, cost, typeof(cost) FROM project
SELECT * FROM project WHERE cost > 200
SELECT * FROM project WHERE code = 'P07'
SELECT * FROM project WHERE code > 'P25'
SELECT * FROM assign WHERE emp_id = 10 AND code = 'P03'
SELECT * FROM emp WHERE dept_id = 2 AND salary > 70000 AND salary <= 130000
SELECT * FROM emp WHERE id IN (3, 5, 7, 300, NULL)
SELECT * FROM emp WHERE id > 115 OR id < 3
SELECT * FROM emp WHERE id BETWEEN 10 AND 14
SELECT * FROM emp WHERE rowid = 12
SELECT * FROM emp WHERE id = '12'
SELECT * FROM emp WHERE id > '100'
SELECT * FROM emp WHERE dept_id = '3'
SELECT * FROM emp WHERE salary IN (SELECT max(salary) FROM emp GROUP BY dept_id)
SELECT e1.id, e2.id FROM emp e1 JOIN emp e2 ON e1.salary = e2.salary AND e1.id < e2.id
SELECT count(*) FROM emp e JOIN assign a ON a.emp_id = e.id JOIN project p ON p.code = a.code WHERE p.dept_id = e.dept_id
SELECT e.dept_id, p.code FROM emp e LEFT JOIN project p ON p.lead_id = e.id WHERE e.id < 30
SELECT length(group_concat(name)) FROM emp
SELECT sum(length(name)), avg(length(name)) FROM emp
SELECT id FROM emp WHERE NOT (salary > 80000 AND dept_id = 4)
SELECT id FROM emp WHERE salary > 80000 OR bonus > 4500
SELECT id FROM emp WHERE (salary > 80000) IS NULL
SELECT id, salary IS NULL, salary IS NOT NULL, salary ISNULL, salary NOTNULL FROM emp WHERE id < 30
SELECT 1 WHERE EXISTS (SELECT 1 FROM emp)
SELECT (SELECT 1), (SELECT NULL), (SELECT 1 WHERE 0)
SELECT id FROM emp WHERE salary > ALL_VALUES_DOES_NOT_EXIST
""".strip()


def cases():
    out = []
    for line in QUERIES.splitlines():
        line = line.strip()
        if not line or "DOES_NOT_EXIST" in line:
            continue
        ordered = line.endswith("-- ordered")
        sql = line.removesuffix("-- ordered").strip()
        out.append(pytest.param(sql, ordered, id=sql[:70]))
    return out


@pytest.mark.parametrize(("sql", "ordered"), cases())
def test_matches_sqlite(oracle, sql, ordered):
    oracle.compare(sql, ordered=ordered)
