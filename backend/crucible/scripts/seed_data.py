"""The seed question bank.

Kept as data, separate from the loader, so questions can be reviewed as
content rather than read out of procedural code.
"""

from __future__ import annotations

from typing import Any

QUESTIONS: list[dict[str, Any]] = [
    # ------------------------------------------------------------- code ---
    {
        "slug": "two-sum-indices",
        "title": "Two Sum",
        "topic": "arrays",
        "type": "code",
        "difficulty": "easy",
        "tags": ["hash-map", "arrays"],
        "prompt": (
            "Given a list of integers and a target, print the **indices** of the two "
            "numbers that add up to the target, space separated, smaller index first.\n\n"
            "**Input**: line 1 is the target. Line 2 is the space-separated list.\n"
            "**Output**: two indices, space separated.\n\n"
            "Exactly one valid answer exists."
        ),
        "constraints_md": "2 <= n <= 10^4\n-10^9 <= nums[i] <= 10^9",
        "allowed_languages": ["python", "javascript", "cpp"],
        "time_limit_ms": 3000,
        "starter_code": {
            "python": "import sys\n\ndata = sys.stdin.read().split('\\n')\ntarget = int(data[0])\nnums = list(map(int, data[1].split()))\n\n# your code here\n",
            "javascript": "const lines = require('fs').readFileSync(0, 'utf8').split('\\n');\nconst target = parseInt(lines[0]);\nconst nums = lines[1].trim().split(/\\s+/).map(Number);\n\n// your code here\n",
        },
        "reference_solution": (
            "import sys\n"
            "data = sys.stdin.read().split('\\n')\n"
            "target = int(data[0]); nums = list(map(int, data[1].split()))\n"
            "seen = {}\n"
            "for i, n in enumerate(nums):\n"
            "    if target - n in seen:\n"
            "        print(seen[target - n], i); break\n"
            "    seen[n] = i\n"
        ),
        "test_cases": [
            {
                "stdin": "9\n2 7 11 15",
                "expected_stdout": "0 1",
                "is_sample": True,
                "explanation": "nums[0] + nums[1] = 2 + 7 = 9",
            },
            {
                "stdin": "6\n3 2 4",
                "expected_stdout": "1 2",
                "is_sample": True,
                "explanation": "nums[1] + nums[2] = 2 + 4 = 6",
            },
            {"stdin": "6\n3 3", "expected_stdout": "0 1"},
            {"stdin": "-8\n-3 4 -5 90", "expected_stdout": "0 2"},
            # 999999998 + 1 = 999999999, which is NOT the target, so (2, 3) is
            # the only pair that sums to it. The prompt promises exactly one
            # valid answer, so the fixture has to honour that -- with
            # 999999999 here, (0, 1) also summed to the target and a correct
            # hash-map solution scored 80%.
            {"stdin": "1000000000\n999999998 1 500000000 500000000", "expected_stdout": "2 3"},
        ],
    },
    {
        "slug": "valid-parentheses",
        "title": "Valid Parentheses",
        "topic": "stacks",
        "type": "code",
        "difficulty": "easy",
        "tags": ["stack", "strings"],
        "prompt": (
            "Given a string containing only `()[]{}`, print `true` if every bracket "
            "is closed by the same type in the correct order, else `false`."
        ),
        "constraints_md": "1 <= len(s) <= 10^4",
        "allowed_languages": ["python", "javascript", "cpp"],
        "test_cases": [
            {"stdin": "()", "expected_stdout": "true", "is_sample": True},
            {
                "stdin": "([)]",
                "expected_stdout": "false",
                "is_sample": True,
                "explanation": "Correct counts, wrong order.",
            },
            {"stdin": "{[]}", "expected_stdout": "true"},
            {"stdin": "(", "expected_stdout": "false"},
            {"stdin": "]", "expected_stdout": "false"},
            {"stdin": "(((((((((())))))))))", "expected_stdout": "true"},
        ],
    },
    {
        "slug": "longest-substring-no-repeat",
        "title": "Longest Substring Without Repeating Characters",
        "topic": "sliding-window",
        "type": "code",
        "difficulty": "medium",
        "tags": ["sliding-window", "strings", "hash-map"],
        "prompt": (
            "Print the length of the longest substring of the input that contains "
            "no repeated characters."
        ),
        "constraints_md": "0 <= len(s) <= 5 * 10^4\nA naive O(n^2) solution will time out on the largest case.",
        "allowed_languages": ["python", "javascript", "cpp"],
        "time_limit_ms": 4000,
        "test_cases": [
            {
                "stdin": "abcabcbb",
                "expected_stdout": "3",
                "is_sample": True,
                "explanation": "'abc'",
            },
            {"stdin": "bbbbb", "expected_stdout": "1", "is_sample": True},
            {"stdin": "pwwkew", "expected_stdout": "3"},
            {
                "stdin": "dvdf",
                "expected_stdout": "3",
                "explanation": "Catches solutions that reset the window too far back.",
            },
            {"stdin": "", "expected_stdout": "0"},
        ],
    },
    {
        "slug": "merge-intervals",
        "title": "Merge Intervals",
        "topic": "sorting",
        "type": "code",
        "difficulty": "medium",
        "tags": ["sorting", "intervals", "greedy"],
        "prompt": (
            "Line 1 is n. The next n lines each contain two integers, a start and an end.\n"
            "Merge all overlapping intervals and print each resulting interval on its own "
            "line, in ascending order of start."
        ),
        "constraints_md": "1 <= n <= 10^4",
        "allowed_languages": ["python", "javascript", "cpp"],
        "test_cases": [
            {
                "stdin": "4\n1 3\n2 6\n8 10\n15 18",
                "expected_stdout": "1 6\n8 10\n15 18",
                "is_sample": True,
            },
            {
                "stdin": "2\n1 4\n4 5",
                "expected_stdout": "1 5",
                "is_sample": True,
                "explanation": "Touching intervals merge.",
            },
            {"stdin": "1\n5 5", "expected_stdout": "5 5"},
            {
                "stdin": "3\n1 10\n2 3\n4 5",
                "expected_stdout": "1 10",
                "explanation": "Fully contained intervals.",
            },
        ],
    },
    {
        "slug": "binary-search-rotated",
        "title": "Search in Rotated Sorted Array",
        "topic": "binary-search",
        "type": "code",
        "difficulty": "hard",
        "tags": ["binary-search", "arrays"],
        "prompt": (
            "Line 1 is the target. Line 2 is a rotated sorted array of distinct integers.\n"
            "Print the index of the target, or -1 if absent. Must run in O(log n)."
        ),
        "constraints_md": "1 <= n <= 5000\nAll values distinct.",
        "allowed_languages": ["python", "javascript", "cpp"],
        "test_cases": [
            {"stdin": "0\n4 5 6 7 0 1 2", "expected_stdout": "4", "is_sample": True},
            {"stdin": "3\n4 5 6 7 0 1 2", "expected_stdout": "-1", "is_sample": True},
            {"stdin": "1\n1", "expected_stdout": "0"},
            {"stdin": "5\n5 1 2 3 4", "expected_stdout": "0", "explanation": "Pivot at index 0."},
        ],
    },
    # -------------------------------------------------------------- sql ---
    {
        "slug": "sql-top-earners-per-dept",
        "title": "Highest Earner Per Department",
        "topic": "sql",
        "type": "sql",
        "difficulty": "medium",
        "tags": ["sql", "window-functions", "joins"],
        "prompt": (
            "Return the name, department and salary of the highest-paid employee in each "
            "department, ordered by department name ascending.\n\n"
            "Columns: `name`, `dept`, `salary`."
        ),
        "payload": {
            "schema_sql": (
                "CREATE TABLE employees ("
                "id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary INTEGER);"
            ),
            "seed_sql": (
                "INSERT INTO employees VALUES "
                "(1,'Ana','engineering',150000),"
                "(2,'Bo','engineering',120000),"
                "(3,'Cy','sales',95000),"
                "(4,'Ди','sales',110000),"
                "(5,'Eve','design',105000);"
            ),
            "sample_rows": [
                {"id": 1, "name": "Ana", "dept": "engineering", "salary": 150000},
                {"id": 3, "name": "Cy", "dept": "sales", "salary": 95000},
            ],
            "expected_rows": [
                ["Eve", "design", 105000],
                ["Ana", "engineering", 150000],
                ["Ди", "sales", 110000],
            ],
            "order_matters": True,
        },
    },
    {
        "slug": "sql-second-highest-salary",
        "title": "Second Highest Salary",
        "topic": "sql",
        "type": "sql",
        "difficulty": "easy",
        "tags": ["sql", "aggregation"],
        "prompt": (
            "Return the second-highest distinct salary as a single column named `salary`. "
            "If it does not exist, return NULL."
        ),
        "payload": {
            "schema_sql": "CREATE TABLE salaries (id INTEGER PRIMARY KEY, salary INTEGER);",
            "seed_sql": "INSERT INTO salaries VALUES (1,100),(2,200),(3,200),(4,300);",
            "sample_rows": [{"id": 1, "salary": 100}, {"id": 2, "salary": 200}],
            "expected_rows": [[200]],
        },
    },
    # -------------------------------------------------------------- mcq ---
    {
        "slug": "mcq-index-selection",
        "title": "Which index serves this query?",
        "topic": "databases",
        "type": "mcq",
        "difficulty": "medium",
        "tags": ["databases", "indexing"],
        "prompt": (
            "A table has 50 million rows. This query is slow:\n\n"
            "```sql\nSELECT * FROM orders\nWHERE customer_id = ? AND status = 'shipped'\n"
            "ORDER BY created_at DESC LIMIT 20;\n```\n\n"
            "Which index best serves it?"
        ),
        "payload": {
            "choices": [
                {"key": "a", "text": "CREATE INDEX ON orders (customer_id)"},
                {
                    "key": "b",
                    "text": "CREATE INDEX ON orders (customer_id, status, created_at DESC)",
                },
                {"key": "c", "text": "CREATE INDEX ON orders (created_at DESC)"},
                {"key": "d", "text": "Three separate single-column indexes"},
            ],
            "correct": ["b"],
            "explanation": (
                "Equality columns first, then the sort column. This lets the planner seek "
                "directly to the matching range and read it already ordered, so the LIMIT "
                "stops after 20 rows with no sort step. Separate single-column indexes "
                "force a bitmap combination and then an explicit sort."
            ),
        },
    },
    {
        "slug": "mcq-http-idempotency",
        "title": "Which HTTP methods should be idempotent?",
        "topic": "system-design",
        "type": "mcq",
        "difficulty": "easy",
        "tags": ["http", "api-design"],
        "prompt": "Select every method that is expected to be idempotent.",
        "payload": {
            "choices": [
                {"key": "a", "text": "GET"},
                {"key": "b", "text": "POST"},
                {"key": "c", "text": "PUT"},
                {"key": "d", "text": "DELETE"},
            ],
            "correct": ["a", "c", "d"],
            "allow_partial_credit": True,
            "explanation": (
                "GET, PUT and DELETE are idempotent: repeating them leaves the same state. "
                "POST is not, which is exactly why creating a resource needs an "
                "Idempotency-Key header to make retries safe."
            ),
        },
    },
    {
        "slug": "mcq-container-isolation",
        "title": "What does dropping Linux capabilities protect against?",
        "topic": "security",
        "type": "mcq",
        "difficulty": "hard",
        "tags": ["security", "containers"],
        "prompt": (
            "A container runs untrusted code with `--cap-drop=ALL`. "
            "Which attack does this specifically prevent?"
        ),
        "payload": {
            "choices": [
                {"key": "a", "text": "A fork bomb exhausting host PIDs"},
                {"key": "b", "text": "Mounting the host filesystem via CAP_SYS_ADMIN"},
                {"key": "c", "text": "Exfiltrating data over the network"},
                {"key": "d", "text": "Exhausting host memory"},
            ],
            "correct": ["b"],
            "explanation": (
                "Capabilities gate privileged kernel operations, so dropping them blocks "
                "mount tricks, ptrace and raw sockets. The others need different controls: "
                "--pids-limit for fork bombs, a network namespace for exfiltration, and a "
                "memory cgroup for memory. This is why isolation is layered rather than a "
                "single switch."
            ),
        },
    },
]
