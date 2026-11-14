-- Run with:  quarry < examples/analytics.sql
-- A small sales dataset, then reports that use CTEs, window functions and date functions.

CREATE TABLE sales (id INTEGER PRIMARY KEY, day TEXT NOT NULL, region TEXT NOT NULL, amount REAL NOT NULL);

-- 90 days of generated sales across three regions, with a recursive CTE
INSERT INTO sales (day, region, amount)
WITH RECURSIVE d(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM d WHERE n < 89)
SELECT date('2026-08-01', '+' || n || ' days'),
       CASE n % 3 WHEN 0 THEN 'north' WHEN 1 THEN 'south' ELSE 'west' END,
       round(100 + (n * 37 % 50) + (n % 7) * 12.5, 2)
FROM d;

.mode box

-- monthly totals per region
SELECT strftime('%Y-%m', day) AS month, region, count(*) AS days, round(sum(amount), 2) AS total
FROM sales GROUP BY month, region ORDER BY month, region;

-- running total and 7-day moving average for one region
SELECT day, amount,
       round(sum(amount) OVER (ORDER BY day), 2) AS running,
       round(avg(amount) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS avg7
FROM sales WHERE region = 'north' ORDER BY day LIMIT 8;

-- best day of each month
WITH ranked AS (
  SELECT strftime('%m', day) AS month, day, amount,
         row_number() OVER (PARTITION BY strftime('%m', day) ORDER BY amount DESC, day) AS rn
  FROM sales
)
SELECT month, day, amount FROM ranked WHERE rn = 1 ORDER BY month;
