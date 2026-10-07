"""The offline table definitions (devtools/tables.py) must match template.yaml,
otherwise tests and the local server would pass against a schema AWS doesn't have."""
from pathlib import Path

import yaml

from devtools import tables


class _CfnLoader(yaml.SafeLoader):
    pass


# Treat CloudFormation short-form tags (!Ref, !Sub, !GetAtt, ...) as plain values.
_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _normalise(spec: dict) -> dict:
    def index_set(indexes):
        return {(i["IndexName"], tuple((k["AttributeName"], k["KeyType"]) for k in i["KeySchema"]),
                 i["Projection"]["ProjectionType"]) for i in indexes or []}
    return {
        "attrs": {(a["AttributeName"], a["AttributeType"]) for a in spec["AttributeDefinitions"]},
        "keys": tuple((k["AttributeName"], k["KeyType"]) for k in spec["KeySchema"]),
        "gsi": index_set(spec.get("GlobalSecondaryIndexes")),
        "lsi": index_set(spec.get("LocalSecondaryIndexes")),
    }


def test_offline_tables_match_template():
    template = yaml.load((Path(__file__).resolve().parents[1] / "template.yaml").read_text(), Loader=_CfnLoader)
    resources = template["Resources"]
    for logical, spec in tables.definitions().items():
        assert logical in resources, logical
        assert _normalise(resources[logical]["Properties"]) == _normalise(spec), logical
