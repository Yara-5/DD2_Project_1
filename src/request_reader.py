#!/usr/bin/env python3
"""Step 1 (handout section 3.3): read and validate a cell request.

The request may give the function as an expression, a hexadecimal truth table,
or both; both must agree. The truth table is the normalized representation:
bit i is the output for the input row whose index is sum(value(inputs[k]) << k),
so inputs[0] is the least significant bit. Writes the normalized request as JSON
(stdout, or --out) and exits 1 on a refused request."""

import argparse
import json
import re
import sys
from pathlib import Path


class RequestError(ValueError):
    """Raised when a cell request is invalid."""


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Boolean expression parser
TOKEN_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[!&|^()])")


class BooleanParser:
    """Recursive descent parser with the precedence !, &, ^, |"""

    def __init__(self, expression, input_names):
        self.expression = expression
        self.input_names = set(input_names)
        self.tokens = self._tokenize(expression)
        self.position = 0

    @staticmethod
    def _tokenize(expression):
        tokens = []
        position = 0

        while position < len(expression):
            # trailing whitespace
            if expression[position:].isspace():
                break

            match = TOKEN_RE.match(expression, position)
            if not match:
                raise RequestError(f"Invalid character in expression at position {position}.")

            tokens.append(match.group(1))
            position = match.end()

        if not tokens:
            raise RequestError("The expression cannot be empty.")

        return tokens

    def _peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def _take(self, expected=None):
        token = self._peek()

        if token is None:
            raise RequestError("Unexpected end of expression.")

        if expected is not None and token != expected:
            raise RequestError(
                f"Expected {expected!r}, but found {token!r}."
            )

        self.position += 1
        return token

    def parse(self):
        tree = self._parse_or()

        if self._peek() is not None:
            raise RequestError(
                f"Unexpected token {self._peek()!r} in expression."
            )

        return tree

    def _parse_or(self):
        node = self._parse_xor()

        while self._peek() == "|":
            self._take("|")
            node = ("or", node, self._parse_xor())

        return node

    def _parse_xor(self):
        node = self._parse_and()

        while self._peek() == "^":
            self._take("^")
            node = ("xor", node, self._parse_and())

        return node

    def _parse_and(self):
        node = self._parse_unary()

        while self._peek() == "&":
            self._take("&")
            node = ("and", node, self._parse_unary())

        return node

    def _parse_unary(self):
        if self._peek() == "!":
            self._take("!")
            return ("not", self._parse_unary())

        if self._peek() == "(":
            self._take("(")
            node = self._parse_or()
            self._take(")")
            return node

        token = self._take()

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            raise RequestError(f"Expected an input name, found {token!r}.")

        if token not in self.input_names:
            raise RequestError(
                f"Unknown input {token!r}; declared inputs are "
                f"{sorted(self.input_names)}."
            )

        return ("input", token)


def evaluate(tree, values):
    """Evaluate a parsed expression for a dictionary of Boolean inputs."""
    operation = tree[0]

    if operation == "input":
        return bool(values[tree[1]])

    if operation == "not":
        return not evaluate(tree[1], values)

    left = evaluate(tree[1], values)
    right = evaluate(tree[2], values)

    if operation == "and":
        return left and right
    if operation == "or":
        return left or right
    if operation == "xor":
        return left != right

    raise RequestError(f"Unknown operation {operation!r}.")


# Truth-table handling
def parse_truth_table(raw_value, input_count):
    """Parse a hexadecimal truth table and validate its bit width."""
    if isinstance(raw_value, bool):
        raise RequestError("truth_table must be a hexadecimal string or integer.")

    if isinstance(raw_value, int):
        if raw_value < 0:
            raise RequestError("truth_table cannot be negative.")
        value = raw_value
    elif isinstance(raw_value, str):
        # int(..., 16) also accepts "_" separators and surrounding signs; allow only hex digits.
        if not re.fullmatch(r"\s*(0[xX])?[0-9a-fA-F]+\s*", raw_value):
            raise RequestError("truth_table must be a hexadecimal value, e.g. '0x7'.")
        value = int(raw_value.strip(), 16)
    else:
        raise RequestError("truth_table must be a hexadecimal string or integer.")

    bit_count = 1 << input_count
    if value < 0 or value >= (1 << bit_count):
        raise RequestError(f"truth_table has bits outside the {bit_count}-row truth table.")

    return value


def expression_truth_table(tree, inputs):
    result = 0

    for combination in range(1 << len(inputs)):
        values = {
            name: bool((combination >> bit_index) & 1)
            for bit_index, name in enumerate(inputs)
        }

        if evaluate(tree, values):
            result |= 1 << combination

    return result


def function_truth_table(expression, inputs):
    """Truth table of an expression in this format, e.g. a Liberty 'function'."""
    return expression_truth_table(BooleanParser(expression, inputs).parse(), inputs)


def output_bit(truth_table, values, inputs):
    """Expected output for a {name: 0/1} input assignment."""
    row = sum(int(values[name]) << index for index, name in enumerate(inputs))
    return (truth_table >> row) & 1


def format_truth_table(value, input_count):
    # truth table with enough digits for all rows
    digits = max(1, ((1 << input_count) + 3) // 4)
    return f"0x{value:0{digits}X}"


# Request validation

REQUIRED_FIELDS = (
    "request_id",
    "name",
    "inputs",
    "output",
    "sizing",
)


def validate_request(request):
    if not isinstance(request, dict):
        raise RequestError("The request must be a JSON object.")

    for field in REQUIRED_FIELDS:
        if field not in request:
            raise RequestError(f"Missing required field: {field!r}.")

    if not isinstance(request["request_id"], str) or not request["request_id"]:
        raise RequestError("request_id must be a non-empty string.")

    if not isinstance(request["name"], str) or not IDENTIFIER_RE.fullmatch(request["name"]):
        raise RequestError("name must be an identifier, e.g. 'dd2_g5__nand2_1'.")

    inputs = request["inputs"]
    if not isinstance(inputs, list) or not all(isinstance(name, str) and IDENTIFIER_RE.fullmatch(name) for name in inputs):
        raise RequestError("inputs must be a list of identifiers.")

    if not 1 <= len(inputs) <= 4:
        raise RequestError(f"The tool builds functions of 1 to 4 inputs; this request has {len(inputs)}.")

    if len(set(inputs)) != len(inputs):
        raise RequestError("Input names must be unique.")

    output = request["output"]
    if not isinstance(output, str) or not IDENTIFIER_RE.fullmatch(output):
        raise RequestError("output must be an identifier.")

    if output in inputs:
        raise RequestError("The output name must differ from every input name.")

    sizing = request["sizing"]
    # 'hd' and 'stack' are the handout's two ways; 'opt' is the group's third family
    # (docs/TECHNICAL.md section 7.1): x1 output stage, internal stages sized by Elmore delay.
    if sizing not in ("hd", "stack", "opt"):
        raise RequestError("sizing must be 'hd', 'stack' or 'opt'.")

    if "estimated_area_um2" in request:
        area = request["estimated_area_um2"]
        if (isinstance(area, bool) or not isinstance(area, (int, float)) or area < 0):
            raise RequestError("estimated_area_um2 must be a non-negative number.")

    has_expression = (isinstance(request.get("expression"), str) and bool(request["expression"].strip()))
    has_truth_table = ("truth_table" in request and request["truth_table"] is not None)

    if not has_expression and not has_truth_table:
        raise RequestError("Provide at least one of 'expression' or 'truth_table'.")

    expression_tree = None
    computed_table = None

    if has_expression:
        expression_tree = BooleanParser(request["expression"], inputs).parse()
        computed_table = expression_truth_table(expression_tree, inputs)

    supplied_table = None
    if has_truth_table:
        supplied_table = parse_truth_table(request["truth_table"], len(inputs))

    if (computed_table is not None
        and supplied_table is not None
        and computed_table != supplied_table):
        raise RequestError(
            "The expression and truth_table describe different functions: "
            f"expression gives {format_truth_table(computed_table, len(inputs))}, "
            f"but truth_table gives {format_truth_table(supplied_table, len(inputs))}."
        )

    normalized_table = (supplied_table if supplied_table is not None else computed_table)

    # Inv and Nand2 recognition
    recognized = None
    if len(inputs) == 1 and normalized_table == 0b01:
        recognized = "inv"
    elif len(inputs) == 2 and normalized_table == 0b0111:
        recognized = "nand2"

    normalized = dict(request)
    normalized["truth_table"] = format_truth_table(normalized_table, len(inputs))
    normalized["truth_table_value"] = normalized_table
    normalized["recognized"] = recognized
    normalized["supported_by_prototype"] = recognized is not None

    if not has_expression:
        normalized.pop("expression", None)

    return normalized


def main():
    parser = argparse.ArgumentParser(
        description="Validate a Boolean function cell request."
    )
    parser.add_argument("request_json", type=Path)
    parser.add_argument("--out", type=Path, help="Also write the result to this JSON file")
    args = parser.parse_args()

    try:
        with args.request_json.open("r", encoding="utf-8") as file:
            request = json.load(file)

        # Force internal name to include prefix and match filename stem
        cell_stem = args.request_json.stem
        prefix = "dd2_g5__"
        
        if cell_stem.startswith(prefix):
            request["name"] = cell_stem
        else:
            request["name"] = f"{prefix}{cell_stem}"

        result = {"status": "pass", "reason": None, "request": validate_request(request)}
    except (OSError, json.JSONDecodeError, RequestError) as exc:
        result = {"status": "fail", "reason": str(exc)}

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if result["status"] == "pass" else 1

if __name__ == "__main__":
    sys.exit(main())
