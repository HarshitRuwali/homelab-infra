"""clean_tool_schemas must make LM Studio-hostile tool schemas compilable without
touching anything else.

Run:  python tests/test_schema.py
Exit 0 = all pass. No pytest dependency, same as test_gate.py.
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from proxy import clean_tool_schemas  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def tool(name, params):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": params}}


# OpenCode's grep tool: a *parameter* called "pattern" must survive.
grep = tool("grep", {"type": "object", "properties": {
    "pattern": {"type": "string", "description": "regex"},
    "path": {"type": "string"}}, "required": ["pattern"]})
# Shape of T3 Code's MCP tools: trimmed-string regex inside anyOf.
t3 = tool("t3_thread_send", {"type": "object", "properties": {
    "threadId": {"anyOf": [{"type": "string", "maxLength": 256,
                            "pattern": "^\\S[\\s\\S]*\\S$|^\\S$|^$"}, {"type": "null"}]}}})
# Shape of Hermes' clarify: array of long strings.
clarify = tool("clarify", {"type": "object", "properties": {
    "choices": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 8000}}},
    "$defs": {"X": {"type": "string", "minLength": 1}}})

data = {"model": "m", "tools": [copy.deepcopy(grep), t3, clarify]}
n = clean_tool_schemas(data)
g, t, c = (x["function"]["parameters"] for x in data["tools"])
check(g == grep["function"]["parameters"], "grep: parameter named 'pattern' kept, schema unchanged")
check(g["required"] == ["pattern"], "grep: required list untouched")
s = t["properties"]["threadId"]["anyOf"][0]
check("pattern" not in s and "maxLength" not in s and s["type"] == "string", "t3: pattern and maxLength dropped inside anyOf")
arr = c["properties"]["choices"]
check("maxItems" not in arr and "maxLength" not in arr["items"], "clarify: maxItems and items.maxLength dropped")
check("minLength" not in c["$defs"]["X"], "$defs are cleaned")
check(n == 5, f"count of dropped keywords is 5 (got {n})")

plain = {"model": "m", "messages": [{"role": "user", "content": "maxLength pattern"}]}
before = copy.deepcopy(plain)
check(clean_tool_schemas(plain) == 0 and plain == before, "request without tools untouched")
check(clean_tool_schemas({"tools": [{"type": "function"}, "junk", None]}) == 0, "malformed tools ignored")

# No-argument tools (Pi under T3) send an object schema without "properties"; LM Studio 400s on it.
noargs = {"tools": [tool("a", {"type": "object"}), {"type": "function", "function": {"name": "b"}},
                    tool("c", {"type": "object", "properties": {"x": {"type": "string"}}})]}
n = clean_tool_schemas(noargs)
a, b, c2 = (t["function"]["parameters"] for t in noargs["tools"])
check(a == {"type": "object", "properties": {}}, "object schema without properties gets an empty properties")
check(b == {"type": "object", "properties": {}}, "missing parameters becomes a minimal object schema")
check(c2["properties"] == {"x": {"type": "string"}}, "existing properties untouched")
check(n == 2, f"two no-argument fixes counted (got {n})")

print("FAIL" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
