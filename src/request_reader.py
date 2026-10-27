
#!/usr/bin/env python3
"""Read and validate Boolean function cell requests.
The request may specify the function using: an expression only, a hexadecimal truth table only or both
I'm considering the truth table to be the normalized function representation."""

import argparse
import json
import re
import sys
from pathlib import Path


class RequestError(ValueError):
    """Raised when a cell request is invalid."""


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
        try:
            # hexadecimal truth table
            value = int(raw_value.strip(), 16)
        except ValueError as exc:
            raise RequestError("truth_table must be a hexadecimal value, e.g. '0x7'.") from exc
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

    if not isinstance(request["name"], str) or not request["name"]:
        raise RequestError("name must be a non-empty string.")

    inputs = request["inputs"]
    if not isinstance(inputs, list) or not all(isinstance(name, str) and name for name in inputs):
        raise RequestError("inputs must be a list of non-empty strings.")

    if not 1 <= len(inputs) <= 4:
        raise RequestError("This prototype supports 1 to 4 inputs.")

    if len(set(inputs)) != len(inputs):
        raise RequestError("Input names must be unique.")

    output = request["output"]
    if not isinstance(output, str) or not output:
        raise RequestError("output must be a non-empty string.")

    if output in inputs:
        raise RequestError("The output name must differ from every input name.")

    sizing = request["sizing"]
    if sizing not in ("hd", "stack"):
        raise RequestError("sizing must be either 'hd' or 'stack'.")

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
    args = parser.parse_args()

    try:
        with args.request_json.open("r", encoding="utf-8") as file:
            request = json.load(file)

        normalized = validate_request(request)
        print(json.dumps({
            "status": "pass",
            "reason": None,
            "request": normalized,
        }, indent=2))

    except (OSError, json.JSONDecodeError, RequestError) as exc:
        print(json.dumps({
            "status": "fail",
            "reason": str(exc),
        }, indent=2))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
