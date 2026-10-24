"""T007: IAM-style evaluation of the function role's statements (specs/008 R4)."""

from edge import policy

ROLE = "arn:aws-us-gov:sts::000000000000:assumed-role/sytline-connect-DirectFunctionRole/sytline-connect-fn"
KEY = "arn:aws-us-gov:kms:us-gov-east-1:000000000000:key/aaaa-1"


def allow(action, resource):
    return {"Effect": "Allow", "Action": action if isinstance(action, list) else [action],
            "Resource": resource if isinstance(resource, list) else [resource]}


def test_exact_allow():
    assert policy.evaluate([allow("kms:Decrypt", KEY)], "kms:Decrypt", KEY)[0] is True


def test_no_statement_means_denied():
    ok, why = policy.evaluate([], "kms:Decrypt", KEY)
    assert ok is False and "no identity-based policy allows" in why


def test_resource_must_match():
    assert policy.evaluate([allow("kms:Decrypt", KEY)], "kms:Decrypt", KEY.replace("aaaa-1", "bbbb-2"))[0] is False


def test_action_must_match():
    assert policy.evaluate([allow("kms:Decrypt", KEY)], "kms:Encrypt", KEY)[0] is False


def test_wildcards_in_action_and_resource():
    s = [allow("dynamodb:*", "arn:aws-us-gov:dynamodb:us-gov-east-1:000000000000:table/t*")]
    assert policy.evaluate(s, "dynamodb:PutItem", "arn:aws-us-gov:dynamodb:us-gov-east-1:000000000000:table/t1")[0]
    assert not policy.evaluate(s, "dynamodb:PutItem", "arn:aws-us-gov:dynamodb:us-gov-east-1:000000000000:table/x1")[0]


def test_action_names_are_case_insensitive_resources_are_not():
    assert policy.evaluate([allow("KMS:decrypt", KEY)], "kms:Decrypt", KEY)[0]
    assert not policy.evaluate([allow("kms:Decrypt", KEY)], "kms:Decrypt", KEY.upper())[0]


def test_explicit_deny_beats_allow():
    s = [allow("kms:*", "*"), {"Effect": "Deny", "Action": ["kms:Decrypt"], "Resource": [KEY]}]
    ok, why = policy.evaluate(s, "kms:Decrypt", KEY)
    assert ok is False and "explicit deny" in why


def test_denial_message_is_worded_like_aws():
    text = policy.denial_message(ROLE, "kms:Decrypt", KEY, "no identity-based policy allows the kms:Decrypt action")
    assert text == (f"User: {ROLE} is not authorized to perform: kms:Decrypt on resource: {KEY} "
                    "because no identity-based policy allows the kms:Decrypt action")


def test_star_resource_matches_anything():
    assert policy.evaluate([allow("s3:GetObject", "*")], "s3:GetObject", "arn:aws-us-gov:s3:::b/k")[0]
