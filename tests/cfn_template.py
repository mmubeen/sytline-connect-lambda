"""Load src/template.yaml with a CloudFormation-aware YAML loader (T001).

Short-form tags become the long-form dicts: `!Ref x` -> {"Ref": "x"}, `!If [..]` -> {"Fn::If": [..]},
`!GetAtt A.B` -> {"Fn::GetAtt": ["A", "B"]}, `!Condition c` -> {"Condition": "c"}.
"""

import os

import yaml

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "template.yaml")


class _Loader(yaml.SafeLoader):
    pass


def _node_value(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


def _make(tag):
    name = tag[1:]

    def construct(loader, node):
        value = _node_value(loader, node)
        if name == "Ref":
            return {"Ref": value}
        if name == "Condition":
            return {"Condition": value}
        if name == "GetAtt":
            return {"Fn::GetAtt": value.split(".", 1) if isinstance(value, str) else value}
        return {f"Fn::{name}": value}

    return construct


for _tag in ("!Ref", "!Sub", "!If", "!Select", "!Cidr", "!GetAZs", "!Split", "!Join", "!Equals", "!Not",
             "!And", "!Or", "!Condition", "!GetAtt", "!Base64", "!FindInMap", "!ImportValue"):
    _Loader.add_constructor(_tag, _make(_tag))


def load_template(path=TEMPLATE_PATH):
    with open(path) as f:
        return yaml.load(f, Loader=_Loader)
