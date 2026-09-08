from src.shader_workspace.quality import organization_quality, organization_quality_issues
from src.shader_workspace.templates import organization_contract, organization_structure_issues


def codes(source):
    return {item["code"] for item in organization_quality_issues(source)}


def test_small_helpers_cannot_hide_anonymous_data_behind_statement_threshold():
    source = "struct Data { float _2323; };\nvoid Compute(inout Data data) { float _9 = data._2323 * 2; data._2323 = _9; }"
    assert organization_contract(source)["needsAlgorithmRestructuring"]
    assert "algorithm-anonymous-values" in codes(source)
    assert "algorithm-anonymous-values" in {item["code"] for item in organization_structure_issues(source, source, {})}


def test_neutral_operation_names_do_not_require_invented_business_meaning():
    source = "float Compute(float x, float y) { float radialDistance = sqrt(x*x + y*y); return radialDistance; }"
    assert organization_quality_issues(source) == []


def test_physical_cbuffer_field_names_are_not_compiler_temporaries():
    source = "cbuffer Original : register(b0) { float _2 : packoffset(c0.x); };\nfloat Compute() { return _2; }"
    assert organization_quality_issues(source) == []
    assert "algorithm-anonymous-values" in codes(source.replace("return _2", "float _2 = 1; return _2"))
    assert "algorithm-anonymous-values" in codes(source.replace("Compute()", "Compute(float _2)"))
    assert "algorithm-anonymous-values" in codes(source.replace("float Compute()", "struct Data { float _2; }; float Compute(Data data)").replace("return _2", "return data._2"))


def test_comment_strings_and_macros_do_not_create_anonymous_local_findings():
    source = '#define RAW_LABEL "_123"\nfloat Compute() { /* float _9; */ // _21\nreturn 1; }'
    assert organization_quality_issues(source) == []


def test_relabeling_temporaries_and_fixed_sized_chunks_is_not_organization():
    body = "\n".join("value = value + 0.0;" for _ in range(30))
    source = "float EvaluateLightStage2(float temp123) { float value = temp123;\n" + body + "\nreturn value; }"
    assert {"algorithm-anonymous-values", "algorithm-mechanical-partition"} <= codes(source)


def test_section_comments_are_inside_real_long_function():
    body = "\n".join("    value = value + 0.0;" for _ in range(42))
    source = "// Long outside description\nfloat Compute(float value) {\n" + body + "\n" + body + "\nreturn value; }"
    assert "algorithm-long-function-sections" in codes(source)
    explained = source.replace(body + "\n" + body, "    // Prepare the captured input contribution.\n" + body +
                               "\n    // Combine the contribution with the output accumulator.\n" + body)
    assert "algorithm-long-function-sections" not in codes(explained)


def test_oversized_expression_is_actionable_warning_not_semantic_failure():
    source = "float Compute(float value) { return " + " + ".join(["value"] * 95) + "; }"
    issues = organization_quality_issues(source)
    assert [item["code"] for item in issues] == ["algorithm-dense-expressions"]
    assert issues[0]["severity"] == "warning"
    assert all(issues[0][key] for key in ("location", "direction", "target", "example"))
    assert organization_quality(source)["policy"]["appliesToIntentionalUserEdits"] is False


def test_warning_volume_cannot_push_a_blocker_out_of_feedback_budget():
    expression = " + ".join(["value"] * 95)
    source = "\n".join("float Helper%d(float value) { return %s; }" % (index, expression) for index in range(70))
    source += "\nfloat Last(float _99) { return _99; }"
    issues = organization_quality_issues(source)
    assert len(issues) == 64
    assert issues[0]["code"] == "algorithm-anonymous-values"
